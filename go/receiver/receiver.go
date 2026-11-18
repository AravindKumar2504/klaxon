package main

// The HTTP side, answered as src/klaxon/patient/services/receiver_app.py answers: the same status codes, bodies and
// headers, with FastAPI's 404 and 405 answers and Starlette's check of the Host header around them.
//
// The receiver books no money and does not dedupe. It proves the request came from the provider, then hands the
// exact signed bytes to Kafka, and answers 200 only after Kafka has acknowledged the write: the provider never hears
// "got it" for an event Kafka does not have. If Kafka is down it answers 503, and the provider tries again later.
// Duplicates pass straight through on purpose: the ledger consumer drops them in the same transaction as the posting.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/metric"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"
)

const (
	maxBodyBytes = 64 * 1024
	webhookRoute = "/webhooks/provider"
	healthRoute  = "/healthz"
	jsonType     = "application/json"
	textType     = "text/plain; charset=utf-8"
	logger       = "klaxon.receiver"
)

// trustedHosts: requests must be addressed to this machine or to the service's Compose name. A web page can aim a
// browser at 127.0.0.1 (DNS rebinding), but its Host header gives it away. "testserver" is FastAPI's test client's.
var trustedHosts = []string{"127.0.0.1", "localhost", "testserver", "webhook-receiver"}

// knownMethods: the methods FastAPI's telemetry records by name. Any other is recorded as _OTHER.
var knownMethods = []string{"CONNECT", "DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT", "QUERY", "TRACE"}

// Publisher writes one message to Kafka and waits for the broker's acknowledgement. It returns the message's
// partition and offset. kafkaPublisher is the real one; the tests pass a fake.
type Publisher interface {
	Publish(topic, key string, value []byte, headers []kafkaHeader) (partition int32, offset int64, err error)
	Healthy() bool
}

type kafkaHeader struct{ name, value string }

// Receiver answers the provider's webhooks and the health check.
type Receiver struct {
	publisher Publisher
	secrets   [][]byte // every secret accepted now: two during a rotation
	topic     string
	tolerance int64            // seconds a signature's timestamp may be from now
	now       func() time.Time // the clock the signature check reads; the tests fix it
	tel       *instruments
	logs      *logs
}

// reply is one answer: written once the request's work is done.
type reply struct {
	status      int
	contentType string
	body        []byte
	allow       string // a 405's Allow header
	retryAfter  string // a 503's Retry-After header
}

func jsonReply(status int, body string) reply {
	return reply{status: status, contentType: jsonType, body: []byte(body)}
}

var (
	badHost  = reply{status: http.StatusBadRequest, contentType: textType, body: []byte("Invalid host header")}
	notFound = jsonReply(http.StatusNotFound, `{"detail":"Not Found"}`)
	// When a request's body cannot be read (the provider hung up), as Python's server answers an error it did not
	// expect.
	serverError = reply{status: http.StatusInternalServerError, contentType: textType, body: []byte("Internal Server Error")}
)

func methodNotAllowed(allow string) reply {
	answer := jsonReply(http.StatusMethodNotAllowed, `{"detail":"Method Not Allowed"}`)
	answer.allow = allow
	return answer
}

func (a reply) write(w http.ResponseWriter) {
	header := w.Header()
	header.Set("Content-Type", a.contentType)
	header.Set("Content-Length", strconv.Itoa(len(a.body)))
	if a.allow != "" {
		header.Set("Allow", a.allow)
	}
	if a.retryAfter != "" {
		header.Set("Retry-After", a.retryAfter)
	}
	w.WriteHeader(a.status)
	w.Write(a.body) // for a HEAD request, net/http sends the headers only
}

// ServeHTTP answers one request. Every request but a health check is traced, measured and logged, as FastAPI's
// telemetry and our request logging do it in Python: a server span that starts a new trace (trace context a caller
// sends is ignored), the request's duration, and one log line with the route's template, never the raw path.
func (rc *Receiver) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == healthRoute { // Docker calls it every 10 s: no trace, no metrics, no log line
		rc.health(r).write(w)
		return
	}
	started := time.Now()
	name, shared, spanOnly := requestAttributes(r)
	ctx, span := rc.tel.tracer.Start(context.WithoutCancel(r.Context()), name, trace.WithNewRoot(),
		trace.WithSpanKind(trace.SpanKindServer), trace.WithAttributes(append(shared, spanOnly...)...))
	active := metric.WithAttributes(shared[:2]...) // the scheme and the method
	rc.tel.active.Add(ctx, 1, active)

	answer, route := badHost, ""
	if trustedHost(r.Host) {
		answer, route = rc.handle(ctx, r, span, name)
	}
	answer.write(w)

	// What FastAPI's telemetry adds once the answer is sent, to the span and to the duration alike.
	finished := slices.Clone(shared)
	if route != "" {
		finished = append(finished, attribute.String("http.route", route))
	}
	finished = append(finished, attribute.Int("http.response.status_code", answer.status))
	if answer.status >= 500 {
		finished = append(finished, attribute.String("error.type", strconv.Itoa(answer.status)))
		span.SetStatus(codes.Error, "")
	}
	span.SetAttributes(finished...)
	rc.tel.duration.Record(ctx, time.Since(started).Seconds(), metric.WithAttributes(finished...))
	rc.tel.active.Add(ctx, -1, active)
	span.End()
}

// handle routes a request addressed to us, answers it, and logs one line about it. It returns the answer and the
// route's template ("" when no route matched).
func (rc *Receiver) handle(ctx context.Context, r *http.Request, span trace.Span, name string) (reply, string) {
	started := time.Now()
	if r.URL.Path != webhookRoute {
		rc.requestLine(ctx, r, "(no matching route)", notFound, started)
		return notFound, ""
	}
	span.SetName(name + " " + webhookRoute)
	answer := methodNotAllowed("POST")
	if r.Method == http.MethodPost {
		var finished bool
		if answer, finished = rc.receive(ctx, r, span); !finished {
			return answer, webhookRoute // its error line is its only line
		}
	}
	rc.requestLine(ctx, r, webhookRoute, answer, started)
	return answer, webhookRoute
}

func (rc *Receiver) requestLine(ctx context.Context, r *http.Request, route string, answer reply, started time.Time) {
	ms := math.Round(time.Since(started).Seconds()*1000*10) / 10
	rc.logs.log(ctx, logger, levelInfo, "request", field{"method", r.Method}, field{"route", route},
		field{"status", answer.status}, field{"ms", ms})
}

// receive is the webhook endpoint: the body's size, the signature, the payload, then Kafka. Each result is counted
// in klaxon.webhooks.received and recorded on the request's span, which tells a rotated secret apart from a broken
// payload. finished is false when the body could not be read.
func (rc *Receiver) receive(ctx context.Context, r *http.Request, server trace.Span) (reply, bool) {
	result := func(name string, answer reply) (reply, bool) {
		rc.tel.webhooks.Add(ctx, 1, metric.WithAttributes(attribute.String("result", name)))
		server.SetAttributes(attribute.String("klaxon.webhook.result", name))
		return answer, true
	}
	body, err := readBody(r, maxBodyBytes)
	if errors.Is(err, errBodyTooLarge) {
		return result("too_large", jsonReply(http.StatusRequestEntityTooLarge, `{"error":"body_too_large"}`))
	}
	if err != nil {
		rc.logs.log(ctx, logger, levelError, "unhandled error", field{"method", r.Method}, field{"route", webhookRoute},
			field{"error", err.Error()})
		return serverError, false
	}
	err = verifyAny(r.Header.Get(signatureHeader), body, rc.secrets, rc.now().Unix(), rc.tolerance)
	if err != nil {
		rc.logs.log(ctx, logger, levelWarning, "webhook refused: bad signature", field{"reason", err.Error()})
		return result("bad_signature", jsonReply(http.StatusBadRequest, `{"error":"bad_signature"}`))
	}
	id, key, err := idAndKey(body)
	if err != nil {
		rc.logs.log(ctx, logger, levelWarning, "webhook refused: bad payload", field{"reason", err.Error()})
		return result("bad_payload", jsonReply(http.StatusBadRequest, `{"error":"bad_payload"}`))
	}
	server.SetAttributes(attribute.String("provider.event_id", id))
	partition, offset, err := rc.publish(ctx, id, key, body)
	if err != nil {
		unavailable := jsonReply(http.StatusServiceUnavailable, `{"error":"queue_unavailable"}`)
		unavailable.retryAfter = "5"
		return result("queue_unavailable", unavailable)
	}
	rc.logs.log(ctx, logger, levelInfo, "webhook queued", field{"event_id", id}, field{"key", key},
		field{"partition", partition}, field{"offset", offset})
	accepted, _ := json.Marshal(struct {
		Received bool   `json:"received"`
		EventID  string `json:"event_id"`
	}{true, id})
	return result("queued", reply{status: http.StatusOK, contentType: jsonType, body: accepted})
}

// publish writes the exact signed bytes to Kafka, keyed by payment, inside a producer span whose trace context goes
// into the message's headers: both consumers continue this webhook's trace under it.
func (rc *Receiver) publish(ctx context.Context, id, key string, body []byte) (int32, int64, error) {
	ctx, span := rc.tel.tracer.Start(ctx, "send "+rc.topic, trace.WithSpanKind(trace.SpanKindProducer),
		trace.WithAttributes(
			attribute.String("messaging.system", "kafka"),
			attribute.String("messaging.operation.type", "send"),
			attribute.String("messaging.destination.name", rc.topic),
			attribute.String("provider.event_id", id),
		))
	defer span.End()
	headers := []kafkaHeader{
		{"content-type", jsonType},
		{"received-at", time.Now().UTC().Format("2006-01-02T15:04:05.000-07:00")},
	}
	headers = append(headers, traceparent(ctx)...)
	partition, offset, err := rc.publisher.Publish(rc.topic, key, body, headers)
	if err != nil {
		span.SetStatus(codes.Error, "Kafka did not confirm the write")
		rc.logs.log(ctx, logger, levelError, "webhook not queued; asking the provider to retry", field{"event_id", id},
			field{"reason", err.Error()})
		return 0, 0, err
	}
	span.SetAttributes(
		attribute.String("messaging.destination.partition.id", strconv.Itoa(int(partition))),
		attribute.Int64("messaging.kafka.offset", offset),
	)
	return partition, offset, nil
}

// traceparent is the current span as a Kafka header: the W3C traceparent only, never tracestate or baggage.
func traceparent(ctx context.Context) []kafkaHeader {
	carrier := propagation.MapCarrier{}
	propagation.TraceContext{}.Inject(ctx, carrier)
	if value, ok := carrier["traceparent"]; ok {
		return []kafkaHeader{{"traceparent", value}}
	}
	return nil // telemetry is off: there is no trace to continue
}

func (rc *Receiver) health(r *http.Request) reply {
	switch {
	case !trustedHost(r.Host):
		return badHost
	case r.Method != http.MethodGet && r.Method != http.MethodHead:
		return methodNotAllowed("GET, HEAD")
	case !rc.publisher.Healthy():
		return jsonReply(http.StatusServiceUnavailable, `{"status":"kafka unreachable"}`)
	}
	return jsonReply(http.StatusOK, `{"status":"ok"}`)
}

var errBodyTooLarge = errors.New("body too large")

// readBody reads at most limit bytes. A body whose declared length is over the limit is refused before any of it is
// read; one sent without a length (in chunks) as soon as it passes the limit.
func readBody(r *http.Request, limit int64) ([]byte, error) {
	if r.ContentLength > limit {
		return nil, errBodyTooLarge
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(body)) > limit {
		return nil, errBodyTooLarge
	}
	return body, nil
}

// trustedHost: the Host header's name, before any ":", is one of trustedHosts (Starlette's TrustedHostMiddleware).
func trustedHost(host string) bool {
	name, _, _ := strings.Cut(host, ":")
	return slices.Contains(trustedHosts, name)
}

// requestAttributes are what FastAPI's telemetry records about a request: the span's name, the attributes the span
// and the duration share, and those of the span only. What a caller chose (the path, the query, a method nobody
// knows, the host's name) is recorded as "redacted", as telemetry.RedactCallerText overwrites it in Python.
func requestAttributes(r *http.Request) (name string, shared, spanOnly []attribute.KeyValue) {
	method, name := r.Method, r.Method
	if !slices.Contains(knownMethods, method) {
		method, name = "_OTHER", "HTTP"
	}
	shared = []attribute.KeyValue{
		attribute.String("url.scheme", "http"),
		attribute.String("http.request.method", method),
		attribute.String("network.protocol.version", fmt.Sprintf("%d.%d", r.ProtoMajor, r.ProtoMinor)),
	}
	spanOnly = []attribute.KeyValue{attribute.String("url.path", "redacted")}
	if port, ok := hostPort(r.Host); ok {
		spanOnly = append(spanOnly, attribute.String("server.address", "redacted"), attribute.Int("server.port", port))
	}
	if r.URL.RawQuery != "" {
		spanOnly = append(spanOnly, attribute.String("url.query", "redacted"))
	}
	if method != r.Method {
		spanOnly = append(spanOnly, attribute.String("http.request.method_original", "redacted"))
	}
	return name, shared, spanOnly
}

// hostPort is the port FastAPI's telemetry records as server.port: the Host header's, 80 when it names none. A Host
// it cannot read gets neither server.address nor server.port.
func hostPort(host string) (int, bool) {
	if host == "" || strings.ContainsAny(host, "@/?#") {
		return 0, false
	}
	if !strings.Contains(host, ":") {
		return 80, true
	}
	_, port, err := net.SplitHostPort(host)
	if err != nil {
		return 0, false
	}
	if port == "" {
		return 80, true
	}
	number, err := strconv.Atoi(port)
	if err != nil || !allDigits(port) || number > 65535 {
		return 0, false
	}
	return number, true
}

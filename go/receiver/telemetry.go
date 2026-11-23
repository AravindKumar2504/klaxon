package main

// OpenTelemetry, as src/klaxon/patient/services/telemetry.py sets it up: traces, metrics and logs sent over OTLP/HTTP
// to grafana/otel-lgtm, with the same resource (service.name, service.namespace, deployment.environment.name), every
// span attribute cut at 300 characters, and no trace context taken from outside. The exporters read the standard
// OTEL_* variables (the endpoint, the metric export interval). Without OTEL_EXPORTER_OTLP_ENDPOINT, or with
// OTEL_SDK_DISABLED=true, nothing is sent: the log lines still go to stdout.

import (
	"context"
	"errors"
	"os"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	otellog "go.opentelemetry.io/otel/log"
	lognoop "go.opentelemetry.io/otel/log/noop"
	"go.opentelemetry.io/otel/metric"
	metricnoop "go.opentelemetry.io/otel/metric/noop"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
	tracenoop "go.opentelemetry.io/otel/trace/noop"
)

const namespace = "klaxon-patient"

// durationBuckets are the bucket bounds FastAPI's telemetry gives http.server.request.duration, in seconds.
var durationBuckets = []float64{0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10}

// providers are where traces, metrics and logs go. shutdown sends what is still buffered.
type providers struct {
	traces   trace.TracerProvider
	metrics  metric.MeterProvider
	logs     otellog.LoggerProvider
	shutdown func(context.Context) error
}

// setupTelemetry makes the providers that send this service's telemetry to OTEL_EXPORTER_OTLP_ENDPOINT, or no-op
// ones when it is not set.
func setupTelemetry(ctx context.Context, service string) (*providers, error) {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" || strings.ToLower(os.Getenv("OTEL_SDK_DISABLED")) == "true" {
		return &providers{tracenoop.NewTracerProvider(), metricnoop.NewMeterProvider(), lognoop.NewLoggerProvider(),
			func(context.Context) error { return nil }}, nil
	}
	res, err := serviceResource(ctx, service)
	if err != nil {
		return nil, err
	}
	spans, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, err
	}
	points, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, err
	}
	records, err := otlploghttp.New(ctx)
	if err != nil {
		return nil, err
	}
	return newProviders(res, spans, sdkmetric.NewPeriodicReader(points), records, true), nil
}

// newProviders makes the three providers for one service, sending to these exporters (telemetry.py's build). With
// batch false they send at once, so a test reads what was sent.
func newProviders(res *resource.Resource, spans sdktrace.SpanExporter, reader sdkmetric.Reader, records sdklog.Exporter,
	batch bool) *providers {
	limits := sdktrace.NewSpanLimits()
	limits.AttributeValueLengthLimit = maxLoggedChars // as in the JSON logs
	sending := sdktrace.WithSyncer(spans)
	var processor sdklog.Processor = sdklog.NewSimpleProcessor(records)
	if batch {
		sending = sdktrace.WithBatcher(spans)
		processor = sdklog.NewBatchProcessor(records)
	}
	traces := sdktrace.NewTracerProvider(sdktrace.WithResource(res), sdktrace.WithRawSpanLimits(limits), sending)
	metrics := sdkmetric.NewMeterProvider(sdkmetric.WithResource(res), sdkmetric.WithReader(reader))
	// A backstop: every log value is already cut at 300 characters (logs.go).
	logs := sdklog.NewLoggerProvider(sdklog.WithResource(res), sdklog.WithAttributeValueLengthLimit(2000),
		sdklog.WithProcessor(processor))
	shutdown := func(ctx context.Context) error {
		return errors.Join(traces.Shutdown(ctx), metrics.Shutdown(ctx), logs.Shutdown(ctx))
	}
	return &providers{traces, metrics, logs, shutdown}
}

// serviceResource is who sends the telemetry. Prometheus names the job "klaxon-patient/webhook-receiver" from it.
func serviceResource(ctx context.Context, service string) (*resource.Resource, error) {
	return resource.New(ctx, resource.WithTelemetrySDK(), resource.WithAttributes(
		attribute.String("service.name", service),
		attribute.String("service.namespace", namespace),
		attribute.String("deployment.environment.name", "local"),
	))
}

// reportSDKErrors puts the SDK's own errors (an export that failed, say) on stdout, never into the pipeline they
// report on.
func reportSDKErrors(l *logs) {
	otel.SetErrorHandler(otel.ErrorHandlerFunc(func(err error) {
		l.log(context.Background(), "opentelemetry", levelError, err.Error())
	}))
}

// instruments are the receiver's own spans and metrics.
type instruments struct {
	tracer   trace.Tracer
	webhooks metric.Int64Counter       // klaxon.webhooks.received, by result
	duration metric.Float64Histogram   // http.server.request.duration
	active   metric.Int64UpDownCounter // http.server.active_requests
}

func newInstruments(p *providers) (*instruments, error) {
	meter := p.metrics.Meter(logger)
	webhooks, err := meter.Int64Counter("klaxon.webhooks.received", metric.WithUnit("{webhook}"),
		metric.WithDescription("Webhook requests, by result."))
	if err != nil {
		return nil, err
	}
	duration, err := meter.Float64Histogram("http.server.request.duration", metric.WithUnit("s"),
		metric.WithDescription("Duration of HTTP server requests."),
		metric.WithExplicitBucketBoundaries(durationBuckets...))
	if err != nil {
		return nil, err
	}
	active, err := meter.Int64UpDownCounter("http.server.active_requests", metric.WithUnit("{request}"),
		metric.WithDescription("Number of active HTTP server requests."))
	if err != nil {
		return nil, err
	}
	return &instruments{p.traces.Tracer(logger), webhooks, duration, active}, nil
}

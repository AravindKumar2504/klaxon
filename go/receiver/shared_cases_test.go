package main

// The shared table of webhook cases (testdata/shared_cases.json), run against the Go receiver. The Python receiver's
// tests run the same file (tests/services/test_receiver_shared_cases.py): both must give each case the same status,
// body and headers, count the same result, and write the same message to Kafka.

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"regexp"
	"slices"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
)

type sharedCase struct {
	Name          string `json:"name"`
	SecretVersion string `json:"secret_version"`
	Kafka         string `json:"kafka"`
	Request       struct {
		Method     string  `json:"method"`
		Path       string  `json:"path"`
		Host       string  `json:"host"`
		Body       string  `json:"body"`
		BodyLatin1 *string `json:"body_latin1"`
		BodyRepeat *struct {
			Text  string `json:"text"`
			Times int    `json:"times"`
		} `json:"body_repeat"`
		Chunked         bool    `json:"chunked"`
		Signature       *string `json:"signature"`
		SignatureLatin1 *string `json:"signature_latin1"`
	} `json:"request"`
	Expect struct {
		Status   int               `json:"status"`
		Body     string            `json:"body"`
		Headers  map[string]string `json:"headers"`
		Result   *string           `json:"result"`
		KafkaKey *string           `json:"kafka_key"`
	} `json:"expect"`
}

type sharedTable struct {
	Now          int64             `json:"now"`
	Secrets      map[string]string `json:"secrets"`
	KafkaHeaders []string          `json:"kafka_headers"`
	Cases        []sharedCase      `json:"cases"`
}

var results = []string{"queued", "bad_signature", "bad_payload", "too_large", "queue_unavailable"}

var receivedAt = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$`) // UTC, to the millisecond

// latin1 is a string's characters as single bytes, as a caller can send them in a header or a body.
func latin1(s string) string {
	out := make([]byte, 0, len(s))
	for _, r := range s {
		out = append(out, byte(r))
	}
	return string(out)
}

func loadSharedTable(t *testing.T) sharedTable {
	t.Helper()
	text, err := os.ReadFile("testdata/shared_cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var table sharedTable
	if err := json.Unmarshal(text, &table); err != nil {
		t.Fatal(err)
	}
	if len(table.Cases) < 8 || table.Now != testNow {
		t.Fatalf("the table has %d cases, at %d", len(table.Cases), table.Now)
	}
	return table
}

func TestTheGoReceiverAnswersEachSharedCase(t *testing.T) {
	table := loadSharedTable(t)
	for _, c := range table.Cases {
		t.Run(c.Name, func(t *testing.T) {
			capture := newCapture(t)
			secrets, err := secretsFromEnv(func(name string) string {
				if name == "WEBHOOK_SECRET_VERSION" {
					return c.SecretVersion
				}
				return table.Secrets[name]
			})
			if err != nil {
				t.Fatal(err)
			}
			kafka := &fakeKafka{down: c.Kafka == "down"}
			if c.Kafka == "down" {
				kafka.fail = errKafkaDown
			}
			receiver := capture.receiver(t, kafka, secrets...)

			body := c.Request.Body
			switch {
			case c.Request.BodyLatin1 != nil:
				body = latin1(*c.Request.BodyLatin1)
			case c.Request.BodyRepeat != nil:
				body = strings.Repeat(c.Request.BodyRepeat.Text, c.Request.BodyRepeat.Times)
			}
			var reader io.Reader = strings.NewReader(body)
			if c.Request.Chunked {
				reader = io.MultiReader(reader) // a body of unknown length, as one sent in chunks
			}
			method, path := c.Request.Method, c.Request.Path
			if method == "" {
				method = http.MethodPost
			}
			if path == "" {
				path = webhookRoute
			}
			request := httptest.NewRequest(method, "http://testserver"+path, reader)
			if c.Request.Host != "" {
				request.Host = c.Request.Host
			}
			switch {
			case c.Request.SignatureLatin1 != nil:
				request.Header.Set(signatureHeader, latin1(*c.Request.SignatureLatin1))
			case c.Request.Signature != nil:
				request.Header.Set(signatureHeader, *c.Request.Signature)
			}

			before := map[string]int64{}
			for _, result := range results {
				before[result] = capture.counter(t, "klaxon.webhooks.received", attribute.String("result", result))
			}
			answer := httptest.NewRecorder()
			receiver.ServeHTTP(answer, request)

			if answer.Code != c.Expect.Status {
				t.Errorf("status %d, want %d", answer.Code, c.Expect.Status)
			}
			if answer.Body.String() != c.Expect.Body {
				t.Errorf("body %q, want %q", answer.Body.String(), c.Expect.Body)
			}
			for name, value := range c.Expect.Headers {
				if got := answer.Header().Get(name); got != value {
					t.Errorf("header %s: %q, want %q", name, got, value)
				}
			}
			for _, result := range results {
				counted := capture.counter(t, "klaxon.webhooks.received", attribute.String("result", result)) - before[result]
				want := int64(0)
				if c.Expect.Result != nil && *c.Expect.Result == result {
					want = 1
				}
				if counted != want {
					t.Errorf("result %s counted %d times, want %d", result, counted, want)
				}
			}
			if c.Expect.KafkaKey == nil {
				if len(kafka.messages) != 0 {
					t.Errorf("%d messages written to Kafka, want none", len(kafka.messages))
				}
				return
			}
			if len(kafka.messages) != 1 {
				t.Fatalf("%d messages written to Kafka, want one", len(kafka.messages))
			}
			message := kafka.messages[0]
			if message.topic != eventsTopic || message.key != *c.Expect.KafkaKey || !bytes.Equal(message.value, []byte(body)) {
				t.Errorf("wrote %s/%s %q, want %s/%s and the exact body", message.topic, message.key, message.value,
					eventsTopic, *c.Expect.KafkaKey)
			}
			var names []string
			for _, h := range message.headers {
				names = append(names, h.name)
			}
			if !slices.Equal(names, table.KafkaHeaders) {
				t.Errorf("headers %v, want %v", names, table.KafkaHeaders)
			}
			if message.headers[0].value != jsonType || !receivedAt.MatchString(message.headers[1].value) {
				t.Errorf("headers %v", message.headers)
			}
		})
	}
}

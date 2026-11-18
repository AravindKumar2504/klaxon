package main

// What the tests share: a fake Kafka, and telemetry kept in memory instead of sent to grafana/otel-lgtm.

import (
	"bytes"
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

const testNow = 1_791_504_000 // the tests' clock, as in the Python tests

var testSecret = []byte("test-secret-not-real")

// published is one message the fake Kafka was given.
type published struct {
	topic, key string
	value      []byte
	headers    []kafkaHeader
}

// fakeKafka records what would have gone to Kafka. fail makes Publish fail as a broker outage does.
type fakeKafka struct {
	mu       sync.Mutex
	messages []published
	fail     error
	down     bool // Healthy says false
}

func (f *fakeKafka) Publish(topic, key string, value []byte, headers []kafkaHeader) (int32, int64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail != nil {
		return 0, 0, f.fail
	}
	f.messages = append(f.messages, published{topic, key, value, headers})
	return 1, int64(len(f.messages) - 1), nil
}

func (f *fakeKafka) Healthy() bool { return !f.down }

var errKafkaDown = publishError("no acknowledgement from Kafka in time")

// memoryLogs keeps the records the logs pipeline exports.
type memoryLogs struct {
	mu      sync.Mutex
	records []sdklog.Record
}

func (m *memoryLogs) Export(_ context.Context, records []sdklog.Record) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	for _, r := range records {
		m.records = append(m.records, r.Clone())
	}
	return nil
}

func (m *memoryLogs) Shutdown(context.Context) error   { return nil }
func (m *memoryLogs) ForceFlush(context.Context) error { return nil }

// capture is one test's telemetry: finished spans, metrics read on demand, exported log records, stdout's lines.
type capture struct {
	spans   *tracetest.InMemoryExporter
	reader  *sdkmetric.ManualReader
	records *memoryLogs
	stdout  *bytes.Buffer
	p       *providers
	logs    *logs
}

// newCapture makes the service's own providers (newProviders), sending at once to memory.
func newCapture(t *testing.T) *capture {
	t.Helper()
	c := &capture{spans: tracetest.NewInMemoryExporter(), reader: sdkmetric.NewManualReader(), records: &memoryLogs{},
		stdout: &bytes.Buffer{}}
	res, err := serviceResource(context.Background(), service)
	if err != nil {
		t.Fatal(err)
	}
	c.p = newProviders(res, c.spans, c.reader, c.records, false)
	if c.logs, err = newLogs(service, "", false, c.p.logs, c.stdout); err != nil {
		t.Fatal(err)
	}
	return c
}

// receiver is a Receiver with this capture's telemetry, the tests' clock and secrets.
func (c *capture) receiver(t *testing.T, kafka Publisher, secrets ...[]byte) *Receiver {
	t.Helper()
	tel, err := newInstruments(c.p)
	if err != nil {
		t.Fatal(err)
	}
	if len(secrets) == 0 {
		secrets = [][]byte{testSecret}
	}
	clock := func() time.Time { return time.Unix(testNow, 0) }
	return &Receiver{kafka, secrets, eventsTopic, defaultToleranceSeconds, clock, tel, c.logs}
}

func (c *capture) metrics(t *testing.T) metricdata.ResourceMetrics {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := c.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatal(err)
	}
	return rm
}

// counter is a counter's total for exactly these attributes (0 before its first count).
func (c *capture) counter(t *testing.T, name string, attrs ...attribute.KeyValue) int64 {
	t.Helper()
	want := attribute.NewSet(attrs...)
	for _, scope := range c.metrics(t).ScopeMetrics {
		for _, m := range scope.Metrics {
			if sum, ok := m.Data.(metricdata.Sum[int64]); ok && m.Name == name {
				for _, point := range sum.DataPoints {
					if point.Attributes.Equals(&want) {
						return point.Value
					}
				}
			}
		}
	}
	return 0
}

// lines are stdout's log lines so far.
func (c *capture) lines() []string {
	return strings.Split(strings.TrimSpace(c.stdout.String()), "\n")
}

// failingBody is a request body whose reading fails, as when the caller hangs up.
type failingBody struct{}

func (failingBody) Read([]byte) (int, error) { return 0, errors.New("unexpected EOF") }

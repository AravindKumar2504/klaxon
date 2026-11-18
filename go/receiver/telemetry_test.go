package main

// The service's telemetry setup, as tests/services/test_telemetry.py checks Python's.

import (
	"context"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
)

func TestEveryAttributeIsCutAt300Characters(t *testing.T) {
	c := newCapture(t)
	_, span := c.p.traces.Tracer("tests").Start(context.Background(), "long")
	span.SetAttributes(attribute.String("note", strings.Repeat("x", 1000)))
	span.End()
	spans := c.spans.GetSpans()
	if len(spans) != 1 || len(spans[0].Attributes[0].Value.AsString()) != 300 {
		t.Fatalf("spans %v", spans)
	}
}

func TestTheResourceNamesTheServiceAsThePythonServicesDo(t *testing.T) {
	c := newCapture(t)
	_, span := c.p.traces.Tracer("tests").Start(context.Background(), "any")
	span.End()
	resource := c.spans.GetSpans()[0].Resource
	want := map[attribute.Key]string{
		"service.name": "webhook-receiver", "service.namespace": "klaxon-patient", "deployment.environment.name": "local",
	}
	for key, value := range want {
		if got, ok := resource.Set().Value(key); !ok || got.AsString() != value {
			t.Errorf("%s: %q", key, got.AsString()) // Prometheus names the job klaxon-patient/webhook-receiver from them
		}
	}
}

func TestTelemetryIsSentOnlyWhenAnEndpointIsSetAndTheSDKIsOn(t *testing.T) {
	cases := []struct {
		endpoint, disabled string
		sent               bool
	}{
		{"", "", false},
		{"http://127.0.0.1:1", "", true}, // nothing is sent until a span ends: no connection here
		{"http://127.0.0.1:1", "TRUE", false},
		{"http://127.0.0.1:1", "false", true},
	}
	for _, c := range cases {
		t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", c.endpoint)
		t.Setenv("OTEL_SDK_DISABLED", c.disabled)
		p, err := setupTelemetry(context.Background(), service)
		if err != nil {
			t.Fatal(err)
		}
		_, span := p.traces.Tracer("tests").Start(context.Background(), "any")
		if span.IsRecording() != c.sent {
			t.Errorf("%+v: recording %v", c, span.IsRecording())
		}
		finish, cancel := context.WithTimeout(context.Background(), time.Second)
		p.shutdown(finish) // nothing listens on port 1: an export that fails is the SDK's to report, not ours
		cancel()
	}
}

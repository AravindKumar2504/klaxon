package main

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// The lines below are the ones Python's JsonFormatter wrote for the same records (run on 2026-11-17).
func TestTheJSONLinesAreWrittenAsPythonWritesThem(t *testing.T) {
	at := time.Unix(testNow, 123987000)
	// Three characters outside ASCII: e with an accent, the line separator U+2028, and an emoji above U+FFFF.
	special := "caf" + string(rune(0xe9)) + " " + string(rune(0x2028)) + " " + string(rune(0x1f600))
	escaped := `caf\` + `u00e9 \` + `u2028 \` + `ud83d\` + `ude00` // as Python's json.dumps writes them
	prefix := `{"ts": "2026-10-09T00:00:00.123+00:00", "level": "%s", "service": "webhook-receiver", "logger": "klaxon.receiver", `
	cases := []struct {
		lv     level
		msg    string
		fields []field
		want   string
	}{
		{levelInfo, "request", []field{{"method", "POST"}, {"route", "/webhooks/provider"}, {"status", 200}, {"ms", 3.0}},
			`"msg": "request", "method": "POST", "route": "/webhooks/provider", "status": 200, "ms": 3.0}`},
		{levelInfo, "request", []field{{"method", "GET"}, {"route", "(no matching route)"}, {"status", 404}, {"ms", 0.4}},
			`"msg": "request", "method": "GET", "route": "(no matching route)", "status": 404, "ms": 0.4}`},
		{levelWarning, "webhook refused: bad signature",
			[]field{{"reason", "quote \" backslash \\ tab \t newline \n " + special + " del \x7f nul \x00"}},
			`"msg": "webhook refused: bad signature", "reason": "quote \" backslash \\ tab \t newline \n ` + escaped + ` del \u007f nul \u0000"}`},
		{levelError, strings.Repeat("x", 301), []field{{"long", strings.Repeat("y", 305)}, {"count", 7}},
			`"msg": "` + strings.Repeat("x", 300) + `...[1 more characters cut]", "long": "` + strings.Repeat("y", 300) +
				`...[5 more characters cut]", "count": 7}`},
	}
	for _, c := range cases {
		got := jsonLine(context.Background(), at, service, logger, c.lv, c.msg, c.fields)
		want := strings.Replace(prefix, "%s", c.lv.name, 1) + c.want
		if got != want {
			t.Errorf("\ngot  %s\nwant %s", got, want)
		}
	}
}

func TestALineInsideASpanCarriesItsTraceAndSpanIds(t *testing.T) {
	ctx, span := sdktrace.NewTracerProvider().Tracer("tests").Start(context.Background(), "process")
	defer span.End()
	line := jsonLine(ctx, time.Unix(testNow, 0), service, logger, levelInfo, "posted", nil)
	ids := span.SpanContext()
	if !strings.Contains(line, `"trace_id": "`+ids.TraceID().String()+`", "span_id": "`+ids.SpanID().String()+`"}`) {
		t.Errorf("%s", line)
	}
	if outside := jsonLine(context.Background(), time.Unix(testNow, 0), service, logger, levelInfo, "posted", nil); strings.Contains(outside, "trace_id") {
		t.Errorf("%s", outside)
	}
}

func TestLinesBelowTheLevelAreDroppedAndTheSDKsOwnStayOnStdout(t *testing.T) {
	c := newCapture(t)
	quiet, err := newLogs(service, "WARNING", true, c.p.logs, c.stdout)
	if err != nil {
		t.Fatal(err)
	}
	quiet.log(context.Background(), logger, levelInfo, "request")
	quiet.log(context.Background(), "opentelemetry", levelError, "export failed")
	quiet.log(context.Background(), logger, levelWarning, "webhook refused: bad payload", field{"reason", "not JSON"})
	if lines := c.lines(); len(lines) != 2 || !strings.Contains(lines[0], "export failed") {
		t.Errorf("stdout %v", lines)
	}
	if len(c.records.records) != 1 || c.records.records[0].Body().AsString() != "webhook refused: bad payload" {
		t.Errorf("pipeline %v", c.records.records) // sending the SDK's own lines would loop
	}
}

func TestLogValuesKeepTheirKindAndStringsAreCut(t *testing.T) {
	values := map[string]attribute.Value{
		"partition": attributeOf(field{"partition", int32(2)}).Value,
		"offset":    attributeOf(field{"offset", int64(124)}).Value,
		"status":    attributeOf(field{"status", 200}).Value,
		"ms":        attributeOf(field{"ms", 1.5}).Value,
		"reason":    attributeOf(field{"reason", strings.Repeat("r", 400)}).Value,
	}
	if values["partition"].AsInt64() != 2 || values["offset"].AsInt64() != 124 || values["status"].AsInt64() != 200 ||
		values["ms"].AsFloat64() != 1.5 || !strings.HasSuffix(values["reason"].AsString(), "...[100 more characters cut]") {
		t.Errorf("%v", values)
	}
	var b bytes.Buffer
	b.WriteString(pythonFloat(12.0) + " " + pythonFloat(0.1) + " " + pythonFloat(1234.5))
	if b.String() != "12.0 0.1 1234.5" {
		t.Errorf("%s", b.String())
	}
}

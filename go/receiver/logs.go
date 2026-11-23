package main

// The service's log lines, as the Python services write them (src/klaxon/patient/services/common.py and
// telemetry.py): one JSON object per line on stdout, and the same record through OpenTelemetry's logs pipeline, where
// Klaxon's search_logs tool reads it. A line inside a trace carries its trace and span ids. Klaxon reads these logs,
// so text a caller chose never goes into them, and every string is cut at 300 characters.

import (
	"context"
	"fmt"
	"io"
	"strconv"
	"strings"
	"sync"
	"time"
	"unicode/utf16"

	"go.opentelemetry.io/otel/attribute"
	otellog "go.opentelemetry.io/otel/log"
	"go.opentelemetry.io/otel/trace"
)

const maxLoggedChars = 300

// level is a Python logging level: its name, its number, and its OpenTelemetry severity.
type level struct {
	name     string
	number   int
	severity otellog.Severity
}

var (
	levelInfo    = level{"INFO", 20, otellog.SeverityInfo}
	levelWarning = level{"WARNING", 30, otellog.SeverityWarn}
	levelError   = level{"ERROR", 40, otellog.SeverityError}
)

// levelNumbers are the names Python's logging takes for LOG_LEVEL.
var levelNumbers = map[string]int{
	"CRITICAL": 50, "FATAL": 50, "ERROR": 40, "WARN": 30, "WARNING": 30, "INFO": 20, "DEBUG": 10, "NOTSET": 0,
}

// field is one structured value of a log line: a string, a whole number or a float.
type field struct {
	key   string
	value any
}

type logs struct {
	service  string
	minimum  int                    // lines below this level are dropped (LOG_LEVEL)
	pipeline otellog.LoggerProvider // OpenTelemetry's logs pipeline: a no-op one when telemetry is off
	mu       sync.Mutex
	out      io.Writer // stdout
}

// newLogs reads LOG_LEVEL as Python's logging does: a level's name, INFO when the setting is unset.
func newLogs(service, setting string, isSet bool, pipeline otellog.LoggerProvider, out io.Writer) (*logs, error) {
	if !isSet {
		setting = "INFO"
	}
	minimum, known := levelNumbers[setting]
	if !known {
		return nil, fmt.Errorf("unknown LOG_LEVEL '%s'", setting)
	}
	return &logs{service: service, minimum: minimum, pipeline: pipeline, out: out}, nil
}

// log writes one line from the named logger (klaxon.receiver, klaxon.kafka). ctx carries the current span, if any.
func (l *logs) log(ctx context.Context, logger string, lv level, msg string, fields ...field) {
	if lv.number < l.minimum {
		return
	}
	now := time.Now()
	l.mu.Lock()
	io.WriteString(l.out, jsonLine(ctx, now, l.service, logger, lv, msg, fields)+"\n")
	l.mu.Unlock()
	if logger == "opentelemetry" {
		return // the SDK's own lines (an export that failed, say) stay on stdout: sending them would loop
	}
	var record otellog.Record
	record.SetTimestamp(now)
	record.SetObservedTimestamp(now)
	record.SetSeverity(lv.severity)
	record.SetSeverityText(lv.name)
	record.SetBody(attribute.StringValue(capped(msg)))
	record.AddAttributes(attribute.String("logger", logger))
	for _, f := range fields {
		record.AddAttributes(attributeOf(f))
	}
	l.pipeline.Logger(logger).Emit(ctx, record) // the SDK adds the trace and span ids from ctx
}

// jsonLine is the line Python's json.dumps writes for the same record: ", " and ": " between items, every
// character outside printable ASCII escaped, and the keys in this order.
func jsonLine(ctx context.Context, at time.Time, service, logger string, lv level, msg string, fields []field) string {
	var b strings.Builder
	item := func(key string, value any) {
		if b.Len() > 1 {
			b.WriteString(", ")
		}
		writeJSONString(&b, key)
		b.WriteString(": ")
		writeJSONValue(&b, value)
	}
	b.WriteString("{")
	item("ts", at.UTC().Format("2006-01-02T15:04:05.000-07:00"))
	item("level", lv.name)
	item("service", service)
	item("logger", logger)
	item("msg", msg) // cut at 300 characters, as every string value is
	if span := trace.SpanContextFromContext(ctx); span.IsValid() {
		item("trace_id", span.TraceID().String())
		item("span_id", span.SpanID().String())
	}
	for _, f := range fields {
		item(f.key, f.value)
	}
	b.WriteString("}")
	return b.String()
}

// capped cuts a string at 300 characters and says how many were cut, as common.capped does.
func capped(s string) string {
	runes := []rune(s)
	if len(runes) <= maxLoggedChars {
		return s
	}
	return fmt.Sprintf("%s...[%d more characters cut]", string(runes[:maxLoggedChars]), len(runes)-maxLoggedChars)
}

func attributeOf(f field) attribute.KeyValue {
	switch v := f.value.(type) {
	case int:
		return attribute.Int(f.key, v)
	case int32:
		return attribute.Int64(f.key, int64(v))
	case int64:
		return attribute.Int64(f.key, v)
	case float64:
		return attribute.Float64(f.key, v)
	default:
		return attribute.String(f.key, capped(fmt.Sprint(v)))
	}
}

func writeJSONValue(b *strings.Builder, value any) {
	switch v := value.(type) {
	case int:
		b.WriteString(strconv.Itoa(v))
	case int32:
		b.WriteString(strconv.FormatInt(int64(v), 10))
	case int64:
		b.WriteString(strconv.FormatInt(v, 10))
	case float64:
		b.WriteString(pythonFloat(v))
	default:
		writeJSONString(b, capped(fmt.Sprint(v)))
	}
}

// pythonFloat writes a float as Python's repr does for the values logged here (durations in milliseconds): the
// shortest digits that read back the same, and always a decimal point, so 3.0 stays 3.0.
func pythonFloat(v float64) string {
	s := strconv.FormatFloat(v, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}

// writeJSONString escapes as Python's json.dumps does by default (ensure_ascii): \" \\ \n \r \t \b \f, and \uXXXX
// for every other character outside printable ASCII, as a surrogate pair above U+FFFF.
func writeJSONString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r >= 0x20 && r <= 0x7e:
				b.WriteRune(r)
			case r > 0xffff:
				high, low := utf16.EncodeRune(r)
				fmt.Fprintf(b, `\u%04x\u%04x`, high, low)
			default:
				fmt.Fprintf(b, `\u%04x`, r)
			}
		}
	}
	b.WriteByte('"')
}

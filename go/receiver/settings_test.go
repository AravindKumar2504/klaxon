package main

// The receiver's settings, as tests/services/test_receiver.py checks Python's (week 4, item 4: fault 5 deploys v2
// alone; the provider signs with v1).

import (
	"slices"
	"strings"
	"testing"
)

var (
	v1, v2 = "v1-secret-not-real", "v2-secret-not-real"
	env    = map[string]string{"WEBHOOK_SECRET_V1": v1, "WEBHOOK_SECRET_V2": v2}
)

func getenv(values map[string]string) func(string) string {
	return func(name string) string { return values[name] }
}

func with(extra map[string]string) map[string]string {
	out := map[string]string{}
	for k, v := range env {
		out[k] = v
	}
	for k, v := range extra {
		out[k] = v
	}
	return out
}

func TestTheReceiverTakesTheSecretsOfTheVersionsItIsToldV1ByDefault(t *testing.T) {
	cases := []struct {
		version string
		set     bool
		want    []string
	}{
		{"", false, []string{v1}},
		{"", true, []string{v1}},
		{"v1", true, []string{v1}},
		{"v2", true, []string{v2}},
		{"v1,v2", true, []string{v1, v2}},
		{" v2 , v1 ", true, []string{v2, v1}},
		{"v1,,", true, []string{v1}},
	}
	for _, c := range cases {
		values := env
		if c.set {
			values = with(map[string]string{"WEBHOOK_SECRET_VERSION": c.version})
		}
		secrets, err := secretsFromEnv(getenv(values))
		if err != nil {
			t.Fatalf("%q: %v", c.version, err)
		}
		var got []string
		for _, s := range secrets {
			got = append(got, string(s))
		}
		if !slices.Equal(got, c.want) {
			t.Errorf("%q: got %v, want %v", c.version, got, c.want)
		}
	}
}

func TestAVersionItDoesNotKnowOrAMissingSecretStopsTheReceiver(t *testing.T) {
	cases := []struct {
		values map[string]string
		why    string
	}{
		{with(map[string]string{"WEBHOOK_SECRET_VERSION": "v3"}), "WEBHOOK_SECRET_VERSION names 'v3'; the versions are v1, v2"},
		{with(map[string]string{"WEBHOOK_SECRET_VERSION": "v1,V2"}), "WEBHOOK_SECRET_VERSION names 'V2'"},
		{map[string]string{"WEBHOOK_SECRET_V1": v1, "WEBHOOK_SECRET_VERSION": "v2"}, "missing required setting WEBHOOK_SECRET_V2"},
		{map[string]string{"WEBHOOK_SECRET_V1": "", "WEBHOOK_SECRET_VERSION": "v1"}, "missing required setting WEBHOOK_SECRET_V1"},
		{map[string]string{}, "missing required setting WEBHOOK_SECRET_V1"},
		{with(map[string]string{"WEBHOOK_SECRET_VERSION": ","}), "WEBHOOK_SECRET_VERSION names no version"},
	}
	for _, c := range cases {
		_, err := secretsFromEnv(getenv(c.values))
		if err == nil || !strings.Contains(err.Error(), c.why) {
			t.Errorf("%v: got %v, want %q", c.values, err, c.why)
		}
	}
}

func lookup(values map[string]string) func(string) (string, bool) {
	return func(name string) (string, bool) {
		value, ok := values[name]
		return value, ok
	}
}

func TestSettingsAreReadAsPythonReadsThem(t *testing.T) {
	if got, err := settingText(lookup(nil), "KAFKA_BOOTSTRAP", "kafka:9092"); got != "kafka:9092" || err != nil {
		t.Errorf("unset: %q %v", got, err)
	}
	if _, err := settingText(lookup(map[string]string{"KAFKA_BOOTSTRAP": ""}), "KAFKA_BOOTSTRAP", "kafka:9092"); err == nil ||
		err.Error() != "missing required setting KAFKA_BOOTSTRAP" {
		t.Errorf("set but empty: %v", err)
	}
	numbers := map[string]int{"": 300, "45": 45, " 45 ": 45, "+45": 45}
	for text, want := range numbers {
		got, err := settingNumber(lookup(map[string]string{"SIGNATURE_TOLERANCE_SECONDS": text}), "SIGNATURE_TOLERANCE_SECONDS", 300)
		if got != want || err != nil {
			t.Errorf("%q: %d %v", text, got, err)
		}
	}
	for _, bad := range []string{"five", "4.5", " "} {
		if _, err := settingNumber(lookup(map[string]string{"PORT": bad}), "PORT", 8000); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

func TestLogLevelTakesPythonsNames(t *testing.T) {
	for setting, want := range map[string]int{"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40, "CRITICAL": 50} {
		l, err := newLogs(service, setting, true, nil, nil)
		if err != nil || l.minimum != want {
			t.Errorf("%s: %v", setting, err)
		}
	}
	if l, err := newLogs(service, "", false, nil, nil); err != nil || l.minimum != 20 {
		t.Errorf("unset: %v", err)
	}
	for _, bad := range []string{"", "info", "10", "LOUD"} {
		if _, err := newLogs(service, bad, true, nil, nil); err == nil {
			t.Errorf("%q accepted", bad)
		}
	}
}

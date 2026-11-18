package main

import (
	"strings"
	"testing"
)

func TestTheKeyIsThePaymentIdSoOnePaymentsEventsShareAPartition(t *testing.T) {
	cases := []struct{ body, id, key string }{
		{`{"id":"evt_000001","data":{"payment_id":"pay_000001"}}`, "evt_000001", "pay_000001"},
		{`{"id":"evt_000001","data":{"payment_id":"pay 1"}}`, "evt_000001", "evt_000001"},
		{`{"id":"evt_000001","data":{"payment_id":null}}`, "evt_000001", "evt_000001"},
		{`{"id":"evt_000001","data":{"payment_id":7}}`, "evt_000001", "evt_000001"},
		{`{"id":"evt_000001","data":"pay_000001"}`, "evt_000001", "evt_000001"},
		{`{"id":"evt_000001","data":null}`, "evt_000001", "evt_000001"},
		{`{"id":"evt_000001"}`, "evt_000001", "evt_000001"},
		{`{"id":"evt` + `\` + `u005f1","data":{"payment_id":"pay` + `\` + `u005f1"}}`, "evt_1", "pay_1"}, // escapes are read
		{`{"id":"evt_bad!","id":"evt_000002"}`, "evt_000002", "evt_000002"},                              // the last of a key wins
		{` {"id":"evt_000001"} ` + "\n", "evt_000001", "evt_000001"},
		{`{"id":"evt_1","n":1e400}`, "evt_1", "evt_1"}, // a number too big for a float is still JSON
	}
	for _, c := range cases {
		id, key, err := idAndKey([]byte(c.body))
		if err != nil || id != c.id || key != c.key {
			t.Errorf("%s: got %q %q %v, want %q %q", c.body, id, key, err, c.id, c.key)
		}
	}
}

func TestAPayloadThatCannotBeUsedIsRefusedWithItsReason(t *testing.T) {
	cases := []struct{ body, reason string }{
		{`not json`, "not JSON: "},
		{``, "not JSON: "},
		{`{"id":"evt_1"} trailing`, "not JSON: "},
		{"{\"id\":\"evt_\xff\"}", "not JSON: invalid UTF-8"},
		{`[]`, "not a JSON object"},
		{`null`, "not a JSON object"},
		{`"evt_1"`, "not a JSON object"},
		{`{}`, "missing or malformed event id"},
		{`{"id":null}`, "missing or malformed event id"},
		{`{"id":""}`, "missing or malformed event id"},
		{`{"id":1}`, "missing or malformed event id"},
		{`{"id":"evt 1"}`, "missing or malformed event id"},
		{`{"id":"evt_1\n"}`, "missing or malformed event id"},
		{`{"id":"` + strings.Repeat("a", 65) + `"}`, "missing or malformed event id"},
		{`{"ID":"evt_1"}`, "missing or malformed event id"}, // keys are matched exactly, as in Python
	}
	for _, c := range cases {
		_, _, err := idAndKey([]byte(c.body))
		if err == nil || !strings.HasPrefix(err.Error(), c.reason) {
			t.Errorf("%q: got %v, want %q", c.body, err, c.reason)
		}
	}
}

func TestAnIdIsOneTo64PlainCharacters(t *testing.T) {
	for _, good := range []string{"a", "evt_000001", "PAY_1", strings.Repeat("9", 64)} {
		if !isID(good) {
			t.Errorf("%q refused", good)
		}
	}
	for _, bad := range []string{"", strings.Repeat("9", 65), "evt-1", "evt 1", "évt", "evt.1", "\x00"} {
		if isID(bad) {
			t.Errorf("%q accepted", bad)
		}
	}
}

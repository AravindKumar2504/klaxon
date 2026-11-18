package main

// The receiver reads only two things from a provider event: its id, and the Kafka key, as
// src/klaxon/patient/services/events.py's id_and_key reads them. The consumers check everything else.
//
//	{"id": "evt_000001", "type": "payment.succeeded", "created": 1791504000,
//	 "data": {"payment_id": "pay_000001", "amount_cents": 1999, "currency": "usd", ...}}

import (
	"encoding/json"
	"unicode/utf8"
)

// badEvent means the payload cannot be used as it is: the receiver answers 400. Its text is the log line's reason.
type badEvent string

func (e badEvent) Error() string { return string(e) }

// idAndKey returns the event id, and the Kafka key: the payment id, so one payment's events share a partition. An
// event without a usable payment id is keyed by its own id.
func idAndKey(body []byte) (id, key string, err error) {
	if !utf8.Valid(body) {
		return "", "", badEvent("not JSON: invalid UTF-8") // Python's json refuses it too, wherever it is
	}
	var raw json.RawMessage
	if err := json.Unmarshal(body, &raw); err != nil { // checks the whole body is one JSON value
		return "", "", badEvent("not JSON: " + err.Error())
	}
	var event map[string]json.RawMessage
	if err := json.Unmarshal(raw, &event); err != nil || event == nil { // null, a list, a string, a number
		return "", "", badEvent("not a JSON object")
	}
	if json.Unmarshal(event["id"], &id) != nil || !isID(id) {
		return "", "", badEvent("missing or malformed event id")
	}
	var data map[string]json.RawMessage
	var paymentID string
	if json.Unmarshal(event["data"], &data) == nil && json.Unmarshal(data["payment_id"], &paymentID) == nil &&
		isID(paymentID) {
		return id, paymentID, nil
	}
	return id, id, nil
}

// isID: 1 to 64 plain ASCII letters, digits or underscores, such as evt_000001 or pay_000001.
func isID(s string) bool {
	if len(s) < 1 || len(s) > 64 {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

package main

// Webhook signatures: HMAC-SHA256 over "<timestamp>.<raw body>", checked exactly as
// src/klaxon/patient/signatures.py checks them. The header looks like "t=1791504000,v1=<64 hex digits>". The
// timestamp is signed too, so a captured request stops working after five minutes.

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"strings"
)

const (
	signatureHeader         = "Klaxon-Signature"
	defaultToleranceSeconds = 300
	maxTimestampDigits      = 12 // as in Python, where int() of a huge digit string raises
)

// signatureError means the request cannot be proven to come from the provider: answer 400, queue nothing. Its text
// is the reason the log line gives, the same words as Python's.
type signatureError string

func (e signatureError) Error() string { return string(e) }

const (
	errMissingHeader      = signatureError("missing signature header")
	errMalformedHeader    = signatureError("malformed signature header")
	errMalformedTimestamp = signatureError("malformed timestamp")
	errOutsideTolerance   = signatureError("timestamp outside tolerance (possible replay)")
	errMismatch           = signatureError("signature mismatch")
	errNoSecret           = signatureError("no webhook secret configured")
)

// computeSignature is the hex HMAC-SHA256 of "<timestamp>.<body>", the timestamp written as a plain number.
func computeSignature(secret []byte, timestamp int64, body []byte) string {
	mac := hmac.New(sha256.New, secret)
	mac.Write([]byte(strconv.FormatInt(timestamp, 10) + "."))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

// verifySignature returns nil when the header proves that body was signed with secret within tolerance seconds of
// now. A bad header of any shape is a signatureError, never anything else.
func verifySignature(header string, body, secret []byte, now, tolerance int64) error {
	if header == "" {
		return errMissingHeader
	}
	var timestamp int64
	found := false
	var candidates []string
	for _, part := range strings.Split(header, ",") {
		key, value, ok := strings.Cut(trimPythonSpace(part), "=")
		if !ok {
			return errMalformedHeader
		}
		if key == "t" {
			if value == "" || len(value) > maxTimestampDigits || !allDigits(value) {
				return errMalformedTimestamp
			}
			timestamp, _ = strconv.ParseInt(value, 10, 64) // at most 12 digits: it always fits
			found = true
		} else if key == "v1" && isASCII(value) {
			candidates = append(candidates, value) // a value that is not ASCII can never equal a hex digest
		}
	}
	if !found || len(candidates) == 0 {
		return errMalformedHeader
	}
	if now-timestamp > tolerance || timestamp-now > tolerance {
		return errOutsideTolerance
	}
	expected := []byte(computeSignature(secret, timestamp, body))
	for _, candidate := range candidates {
		if hmac.Equal(expected, []byte(candidate)) { // compares in constant time
			return nil
		}
	}
	return errMismatch
}

// verifyAny accepts a signature made with any of our current secrets: during a rotation, two are current. Each
// secret gives the same error up to the comparison, so the last one's is the one to report.
func verifyAny(header string, body []byte, secrets [][]byte, now, tolerance int64) error {
	if len(secrets) == 0 {
		return errNoSecret
	}
	var err error
	for _, secret := range secrets {
		if err = verifySignature(header, body, secret, now, tolerance); err == nil {
			return nil
		}
	}
	return err
}

// trimPythonSpace removes what Python's str.strip() removes from a header value, which Starlette reads as Latin-1:
// ASCII whitespace and the two Latin-1 spaces, 0x85 and 0xA0. Go's strings.TrimSpace would keep those two bytes.
func trimPythonSpace(s string) string {
	start, end := 0, len(s)
	for start < end && isPythonSpace(s[start]) {
		start++
	}
	for end > start && isPythonSpace(s[end-1]) {
		end--
	}
	return s[start:end]
}

func isPythonSpace(b byte) bool {
	return b == ' ' || (b >= '\t' && b <= '\r') || (b >= 0x1c && b <= 0x1f) || b == 0x85 || b == 0xa0
}

func allDigits(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

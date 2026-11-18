package main

// The signature check, case by case, as tests/test_signatures.py checks Python's, and the reason each refusal gives.

import (
	"strconv"
	"strings"
	"testing"
)

var (
	sigSecret = []byte("s3cret")
	sigBody   = []byte(`{"id":"evt_0001"}`)
)

func sign(secret []byte, timestamp int64, body []byte) string {
	return "t=" + itoa(timestamp) + ",v1=" + computeSignature(secret, timestamp, body)
}

func itoa(n int64) string { return strconv.FormatInt(n, 10) }

func TestTheSignatureIsTheProvidersHMAC(t *testing.T) {
	// The provider's own: signatures.sign(b"s3cret", 1791504000, b'{"id":"evt_0001"}'), run in Python.
	want := "t=1791504000,v1=be1bcb788110b44b1480569667fce327dd61f50c8722290bdbdb04a6c72a41b6"
	if got := sign(sigSecret, testNow, sigBody); got != want {
		t.Fatalf("got %s, want %s", got, want)
	}
}

func TestARoundTripIsAccepted(t *testing.T) {
	if err := verifySignature(sign(sigSecret, testNow, sigBody), sigBody, sigSecret, testNow, 300); err != nil {
		t.Fatal(err)
	}
}

func TestWhatCannotBeProvenIsRefusedWithItsReason(t *testing.T) {
	good := sign(sigSecret, testNow, sigBody)
	digest := computeSignature(sigSecret, testNow, sigBody)
	cases := []struct {
		name, header, body string
		now                int64
		want               signatureError
	}{
		{"no header", "", string(sigBody), testNow, errMissingHeader},
		{"another secret", sign([]byte("wrong"), testNow, sigBody), string(sigBody), testNow, errMismatch},
		{"a body changed after signing", good, `{"id":"evt_9999"}`, testNow, errMismatch},
		{"too old: a replay", good, string(sigBody), testNow + 301, errOutsideTolerance},
		{"from the future", good, string(sigBody), testNow - 301, errOutsideTolerance},
		{"garbage", "garbage", string(sigBody), testNow, errMalformedHeader},
		{"empty values", "t=,v1=", string(sigBody), testNow, errMalformedTimestamp},
		{"letters for a timestamp", "t=abc,v1=00", string(sigBody), testNow, errMalformedTimestamp},
		{"no signature at all", "t=" + itoa(testNow), string(sigBody), testNow, errMalformedHeader},
		{"an empty signature", "t=" + itoa(testNow) + ",v1=", string(sigBody), testNow, errMismatch},
		{"half of the right digest", "t=" + itoa(testNow) + ",v1=" + digest[:32], string(sigBody), testNow, errMismatch},
		{"no timestamp", "v1=00", string(sigBody), testNow, errMalformedHeader},
		{"5000 digits", "t=" + strings.Repeat("9", 5000) + ",v1=00", string(sigBody), testNow, errMalformedTimestamp},
		{"13 digits", "t=0" + itoa(testNow) + "00,v1=00", string(sigBody), testNow, errMalformedTimestamp},
		{"a Unicode digit", "t=" + string(rune(0xb2)) + ",v1=00", string(sigBody), testNow, errMalformedTimestamp},
		{"a signature that is not ASCII", "t=" + itoa(testNow) + ",v1=" + string(rune(0xe9)), string(sigBody), testNow,
			errMalformedHeader},
		{"raw bytes that are not ASCII", "t=1,v1=\xff", string(sigBody), testNow, errMalformedHeader},
		{"a part without '='", good + ",extra", string(sigBody), testNow, errMalformedHeader},
		{"a valid part, then a bad timestamp", good + ",t=x", string(sigBody), testNow, errMalformedTimestamp},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := verifySignature(c.header, []byte(c.body), sigSecret, c.now, 300)
			if err != c.want {
				t.Fatalf("got %v, want %v", err, c.want)
			}
		})
	}
}

func TestTheToleranceIsInclusiveAt300Seconds(t *testing.T) {
	header := sign(sigSecret, testNow, sigBody)
	for _, now := range []int64{testNow + 300, testNow - 300} {
		if err := verifySignature(header, sigBody, sigSecret, now, 300); err != nil {
			t.Errorf("at %+d s: %v", now-testNow, err)
		}
	}
}

func TestAnyMatchingV1CandidateIsAccepted(t *testing.T) {
	// During a rotation a provider can send two signatures; one valid one is enough.
	header := "t=" + itoa(testNow) + ",v1=deadbeef,v1=" + computeSignature(sigSecret, testNow, sigBody)
	if err := verifySignature(header, sigBody, sigSecret, testNow, 300); err != nil {
		t.Fatal(err)
	}
}

func TestTheTimestampIsSignedAsItsNumber(t *testing.T) {
	// Python signs str(int(value)): a leading zero in the header does not change the signed text.
	header := "t=0" + itoa(testNow) + ",v1=" + computeSignature(sigSecret, testNow, sigBody)
	if err := verifySignature(header, sigBody, sigSecret, testNow, 300); err != nil {
		t.Fatal(err)
	}
}

func TestPartsAreTrimmedAsPythonsStripTrimsThem(t *testing.T) {
	digest := computeSignature(sigSecret, testNow, sigBody)
	for _, space := range []string{" ", "\t", "\xa0", "\x85", "\x1c"} {
		header := space + "t=" + itoa(testNow) + space + "," + space + "v1=" + digest + space
		if err := verifySignature(header, sigBody, sigSecret, testNow, 300); err != nil {
			t.Errorf("%q around each part: %v", space, err)
		}
	}
	// Inside a part nothing is trimmed: "t =1" is not the timestamp.
	if err := verifySignature("t =1,v1="+digest, sigBody, sigSecret, testNow, 300); err != errMalformedHeader {
		t.Errorf("t =1: %v", err)
	}
	if trimPythonSpace("\xc2\xa0v1") != "\xc2\xa0v1" { // UTF-8's no-break space starts with 0xC2, which stays
		t.Error("0xC2 was trimmed")
	}
}

func TestDuringARotationEitherSecretIsAccepted(t *testing.T) {
	secrets := [][]byte{[]byte("old-secret"), []byte("new-secret")}
	for _, secret := range secrets {
		if err := verifyAny(sign(secret, testNow, sigBody), sigBody, secrets, testNow, 300); err != nil {
			t.Errorf("%s: %v", secret, err)
		}
	}
	retired := sign([]byte("retired-secret"), testNow, sigBody)
	if err := verifyAny(retired, sigBody, secrets, testNow, 300); err != errMismatch {
		t.Errorf("retired: %v", err)
	}
	if err := verifyAny(sign(sigSecret, testNow, sigBody), sigBody, nil, testNow, 300); err != errNoSecret {
		t.Errorf("no secret: %v", err)
	}
}

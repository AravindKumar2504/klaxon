import pytest

from klaxon.patient import signatures
from klaxon.patient.signatures import SignatureError

SECRET = b"s3cret"
BODY = b'{"id":"evt_0001"}'
NOW = 1_791_504_000


def test_round_trip_returns_the_signed_timestamp():
    header = signatures.sign(SECRET, NOW, BODY)
    assert signatures.verify(header, BODY, SECRET, now=NOW) == NOW


@pytest.mark.parametrize(
    ("header", "body", "secret", "now"),
    [
        (None, BODY, SECRET, NOW),  # missing header
        ("", BODY, SECRET, NOW),
        (signatures.sign(b"wrong", NOW, BODY), BODY, SECRET, NOW),  # signed with another secret
        (signatures.sign(SECRET, NOW, BODY), b'{"id":"evt_9999"}', SECRET, NOW),  # body changed after signing
        (signatures.sign(SECRET, NOW, BODY), BODY, SECRET, NOW + 301),  # too old: a replay
        (signatures.sign(SECRET, NOW, BODY), BODY, SECRET, NOW - 301),  # from the future
    ],
)
def test_rejects_requests_it_cannot_prove(header, body, secret, now):
    with pytest.raises(SignatureError):
        signatures.verify(header, body, secret, now=now)


def test_the_tolerance_window_is_inclusive_at_300_seconds():
    header = signatures.sign(SECRET, NOW, BODY)
    assert signatures.verify(header, BODY, SECRET, now=NOW + 300) == NOW
    with pytest.raises(SignatureError):
        signatures.verify(header, BODY, SECRET, now=NOW + 301)


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        "t=,v1=",
        "t=abc,v1=00",
        f"t={NOW}",  # no signature at all
        f"t={NOW},v1=",  # an empty signature
        f"t={NOW},v1={signatures.compute(SECRET, NOW, BODY)[:32]}",  # half of the right digest
        "v1=00",  # no timestamp
        f"t={'9' * 5000},v1=00",  # int() would raise past 4300 digits
        f"t=²,v1=00",  # a Unicode digit that int() rejects
        f"t={NOW},v1=éé",  # non-ASCII: hmac.compare_digest would raise TypeError
        b"t=1,v1=\xff",  # raw header bytes that are not ASCII
        12345,  # not a header at all
    ],
)
def test_malformed_headers_are_rejected_not_a_crash(header):
    # Every bad header must become SignatureError (a 400), never another exception (a 500).
    with pytest.raises(SignatureError):
        signatures.verify(header, BODY, SECRET, now=NOW)


def test_a_valid_header_given_as_raw_bytes_is_accepted():
    header = signatures.sign(SECRET, NOW, BODY).encode()
    assert signatures.verify(header, BODY, SECRET, now=NOW) == NOW


def test_any_matching_v1_candidate_is_accepted():
    # During secret rotation a provider can send two signatures; one valid one is enough.
    good = signatures.compute(SECRET, NOW, BODY)
    assert signatures.verify(f"t={NOW},v1=deadbeef,v1={good}", BODY, SECRET, now=NOW) == NOW

"""Webhook signatures: HMAC-SHA256 over "<timestamp>.<raw body>".

The same shape as Stripe's documented v1 scheme, written from scratch. The
provider signs every delivery; our endpoint verifies before it trusts the body.
The timestamp is signed too, so a captured request stops working after five
minutes. Inside that window a replay looks like a duplicate delivery, which is
one more reason the handler must be idempotent.
"""

from __future__ import annotations

import hashlib
import hmac

HEADER = "Klaxon-Signature"
DEFAULT_TOLERANCE_SECONDS = 300
_MAX_TIMESTAMP_DIGITS = 12  # int() of a huge digit string raises, so cap it first


class SignatureError(ValueError):
    """The request cannot be proven to come from the provider: answer 400, post nothing."""


def compute(secret: bytes, timestamp: int, body: bytes) -> str:
    signed_payload = str(timestamp).encode() + b"." + body
    return hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()


def sign(secret: bytes, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={compute(secret, timestamp, body)}"


def verify(
    header: str | bytes | None,
    body: bytes,
    secret: bytes,
    now: int,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
) -> int:
    """Return the signed timestamp, or raise SignatureError.

    A bad header of any shape (missing, malformed, non-ASCII, raw bytes) must
    end in SignatureError (a 400), never in some other exception (a 500 and a
    crash loop of provider retries).
    """
    if isinstance(header, bytes):  # some servers hand over raw header bytes
        try:
            header = header.decode("ascii")
        except UnicodeDecodeError:
            raise SignatureError("malformed signature header") from None
    if not isinstance(header, str) or not header:
        raise SignatureError("missing signature header")

    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            raise SignatureError("malformed signature header")
        if key == "t":
            if not (value.isascii() and value.isdigit()) or len(value) > _MAX_TIMESTAMP_DIGITS:
                raise SignatureError("malformed timestamp")
            timestamp = int(value)
        elif key == "v1" and value.isascii():
            # compare_digest raises TypeError on non-ASCII str, and a non-ASCII
            # value can never equal a hex digest anyway, so skip it.
            candidates.append(value)

    if timestamp is None or not candidates:
        raise SignatureError("malformed signature header")
    if abs(now - timestamp) > tolerance_seconds:
        raise SignatureError("timestamp outside tolerance (possible replay)")

    expected = compute(secret, timestamp, body)
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise SignatureError("signature mismatch")
    return timestamp

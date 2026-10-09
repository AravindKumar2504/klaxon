import json
import sqlite3

import pytest

from klaxon.patient import signatures
from klaxon.patient.ledger import Ledger, Line, UnbalancedTransaction, WebhookEndpoint
from klaxon.patient.provider import WebhookRequest
from tests.conftest import SECRET, make_endpoint


def signed(provider, payload: dict) -> WebhookRequest:
    body = json.dumps(payload).encode()
    return WebhookRequest(body, {signatures.HEADER: signatures.sign(SECRET, provider.clock.now(), body)})


def test_ledger_refuses_an_unbalanced_transaction():
    ledger = Ledger()
    with pytest.raises(UnbalancedTransaction):
        ledger.post(
            event_id="evt_x",
            payment_id="pay_x",
            description="broken",
            lines=[Line("provider_clearing", "debit", 100), Line("sales", "credit", 99)],
            posted_at=0,
        )
    assert ledger.transactions() == []


@pytest.mark.parametrize("amount", [100.5, 100.0, True, 0, -5, "100"])
def test_ledger_refuses_anything_but_positive_whole_cents(amount):
    ledger = Ledger()
    with pytest.raises(ValueError):
        ledger.post(
            event_id="evt_x",
            payment_id="pay_x",
            description="float money",
            lines=[Line("provider_clearing", "debit", amount), Line("sales", "credit", amount)],
            posted_at=0,
        )
    assert ledger.transactions() == []


def test_the_database_itself_refuses_fractional_cents():
    # STRICT tables: even SQL that bypasses Ledger.post cannot store 100.5 cents.
    ledger = Ledger()
    ledger.conn.execute("INSERT INTO transactions (event_id, payment_id, description, posted_at) VALUES ('e', 'p', 'd', 0)")
    with pytest.raises(sqlite3.IntegrityError):
        ledger.conn.execute("INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES (1, 'sales', 'credit', 100.5)")


def test_foreign_keys_are_on():
    ledger = Ledger()
    assert ledger.conn.execute("PRAGMA foreign_keys").fetchone() == (1,)
    ledger.conn.execute("INSERT INTO transactions (event_id, payment_id, description, posted_at) VALUES ('e', 'p', 'd', 0)")
    with pytest.raises(sqlite3.IntegrityError):  # a typo in an account name is refused
        ledger.conn.execute("INSERT INTO entries (transaction_id, account, side, amount_cents) VALUES (1, 'provider_clearng', 'debit', 5)")


def test_account_balances_sit_on_their_normal_side(provider):
    ledger, endpoint = make_endpoint(provider, "idempotent")
    provider.deliver(endpoint, provider.plan("normal"))
    assert ledger.balance_cents("provider_clearing") == 2_500 + 4_900 + 1_250 - 1_000
    assert ledger.balance_cents("sales") == 2_500 + 4_900 + 1_250  # credit-normal, so positive
    assert ledger.balance_cents("refunds") == 1_000


def test_naive_handler_posts_a_repeated_event_again(provider):
    ledger, endpoint = make_endpoint(provider, "naive")
    request = provider.signed_request(provider.events[1])
    assert endpoint(request) == 200
    assert endpoint(request) == 200
    assert [r.outcome for r in endpoint.requests] == ["posted", "posted"]
    assert len(ledger.transactions()) == 2
    assert ledger.balance_cents("provider_clearing") == 2 * 4_900


def test_idempotent_handler_acknowledges_a_repeat_and_posts_nothing(provider):
    ledger, endpoint = make_endpoint(provider, "idempotent")
    request = provider.signed_request(provider.events[1])
    assert endpoint(request) == 200
    assert endpoint(request) == 200  # still 200, or the provider would keep retrying
    assert [r.outcome for r in endpoint.requests] == ["posted", "already_processed"]
    assert len(ledger.transactions()) == 1
    assert ledger.balance_cents("provider_clearing") == 4_900


def test_processed_events_rejects_a_second_row_for_the_same_event():
    ledger = Ledger()
    ledger.conn.execute("INSERT INTO processed_events (event_id, processed_at) VALUES ('evt_0002', 0)")
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        ledger.conn.execute("INSERT INTO processed_events (event_id, processed_at) VALUES ('evt_0002', 1)")


def test_a_failed_posting_leaves_no_dedupe_record_so_the_retry_still_posts(provider, monkeypatch):
    # The core guarantee: the dedupe row and the posting commit together or not at all.
    # If the dedupe row survived a failed posting, the retry would get "already processed"
    # and the payment would be lost.
    ledger, endpoint = make_endpoint(provider, "idempotent")
    request = provider.signed_request(provider.events[0])
    real_insert = Ledger._insert

    def broken_insert(self, *args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error (simulated)")

    monkeypatch.setattr(Ledger, "_insert", broken_insert)
    assert endpoint(request) == 500
    assert endpoint.requests[-1].error == "OperationalError('disk I/O error (simulated)')"
    assert ledger.conn.execute("SELECT COUNT(*) FROM processed_events").fetchone() == (0,)

    monkeypatch.setattr(Ledger, "_insert", real_insert)
    assert endpoint(request) == 200  # the provider's retry
    assert endpoint.requests[-1].outcome == "posted"
    assert ledger.balance_cents("provider_clearing") == 2_500


def test_dedupe_treats_only_a_repeated_event_id_as_seen_before():
    ledger = Ledger()
    lines = [Line("provider_clearing", "debit", 100), Line("sales", "credit", 100)]
    with pytest.raises(sqlite3.IntegrityError):  # a missing timestamp is an error, not "already processed"
        ledger.post(event_id="evt_new", payment_id="p", description="d", lines=lines, posted_at=None, dedupe=True)
    assert ledger.post(event_id="evt_new", payment_id="p", description="d", lines=lines, posted_at=1, dedupe=True)
    assert not ledger.post(event_id="evt_new", payment_id="p", description="d", lines=lines, posted_at=2, dedupe=True)


def test_a_reversal_mirrors_the_original_and_cannot_be_applied_twice(provider):
    ledger, endpoint = make_endpoint(provider, "naive")
    request = provider.signed_request(provider.events[1])
    endpoint(request)
    endpoint(request)  # counted twice: transactions 1 and 2
    reversal_id = ledger.reverse(2, reason="duplicate delivery of evt_0002", posted_at=99)
    assert ledger.balance_cents("provider_clearing") == 4_900
    reversal = ledger.transactions()[-1]
    assert reversal["id"] == reversal_id and reversal["reverses_transaction_id"] == 2
    assert [(line["account"], line["side"]) for line in reversal["lines"]] == [
        ("provider_clearing", "credit"),
        ("sales", "debit"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        ledger.reverse(2, reason="again", posted_at=100)
    with pytest.raises(ValueError):
        ledger.reverse(reversal_id, reason="reversing a reversal", posted_at=101)
    with pytest.raises(ValueError):
        ledger.reverse(404, reason="no such transaction", posted_at=102)


def test_bad_signature_is_rejected_and_nothing_is_posted(provider):
    ledger, endpoint = make_endpoint(provider, "idempotent")
    good = provider.signed_request(provider.events[0])
    forged = WebhookRequest(
        good.body, {signatures.HEADER: signatures.sign(b"attacker-secret", provider.clock.now(), good.body)}
    )
    tampered = WebhookRequest(good.body.replace(b"2500", b"250000"), good.headers)
    unsigned = WebhookRequest(good.body, {})
    for request in (forged, tampered, unsigned):
        assert endpoint(request) == 400
    assert [r.outcome for r in endpoint.requests] == ["bad_signature"] * 3
    assert ledger.transactions() == []


def test_header_name_is_case_insensitive(provider):
    _, endpoint = make_endpoint(provider, "naive")
    request = provider.signed_request(provider.events[0])
    lowered = WebhookRequest(request.body, {k.lower(): v for k, v in request.headers.items()})
    assert endpoint(lowered) == 200


def _payment(**data) -> dict:
    return {"id": "evt_1", "type": "payment.succeeded", "data": {"payment_id": "p", "amount_cents": 5, "currency": "usd"} | data}


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "evt_1", "type": "payment.succeeded"},  # no data
        _payment(amount_cents=12.5),
        _payment(amount_cents=-5),
        _payment(amount_cents=True),
        _payment(payment_id=7),
        _payment(currency="eur"),  # a single-currency ledger must not book euros as dollars
        _payment(currency=None),
        _payment() | {"id": 7},
    ],
)
def test_signed_but_malformed_payload_is_a_400_not_a_crash(provider, payload):
    ledger, endpoint = make_endpoint(provider, "naive")
    assert endpoint(signed(provider, payload)) == 400
    assert endpoint.requests[-1].outcome == "bad_payload"
    assert ledger.transactions() == []


def test_signed_body_that_is_not_json_is_a_400(provider):
    _, endpoint = make_endpoint(provider, "naive")
    body = b"not json"
    request = WebhookRequest(body, {signatures.HEADER: signatures.sign(SECRET, provider.clock.now(), body)})
    assert endpoint(request) == 400


def test_unknown_event_types_are_acknowledged_and_ignored(provider):
    ledger, endpoint = make_endpoint(provider, "naive")
    assert endpoint(signed(provider, _payment() | {"type": "customer.updated"})) == 200
    assert endpoint.requests[-1].outcome == "ignored_type"
    assert ledger.transactions() == []


def test_unknown_mode_is_an_error():
    with pytest.raises(ValueError):
        WebhookEndpoint(Ledger(), SECRET, "optimistic", now=lambda: 0)

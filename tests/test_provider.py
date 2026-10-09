import pytest

from klaxon.patient import signatures
from klaxon.patient.provider import FakeProvider
from tests.conftest import SECRET


def test_provider_records_are_the_source_of_truth(provider):
    assert [e.id for e in provider.events] == ["evt_0001", "evt_0002", "evt_0003", "evt_0004"]
    assert provider.balance_cents() == 2_500 + 4_900 + 1_250 - 1_000


def test_amounts_must_be_positive_whole_cents():
    p = FakeProvider(SECRET)
    for bad in (0, -100, 12.5, True, "100"):
        with pytest.raises(ValueError):
            p.create_payment("cus_001", bad)


def test_cannot_refund_more_than_was_captured(provider):
    with pytest.raises(ValueError):
        provider.refund("pay_0001", 1_501)  # $25.00 captured, $10.00 already refunded


def test_every_delivery_is_signed_with_the_current_time(provider):
    request = provider.signed_request(provider.events[0])
    header = request.headers[signatures.HEADER]
    assert signatures.verify(header, request.body, SECRET, now=provider.clock.now()) == provider.clock.now()


def test_normal_schedule_sends_each_event_once_in_order(provider):
    plan = provider.plan("normal")
    assert [d.event.id for d in plan] == ["evt_0001", "evt_0002", "evt_0003", "evt_0004"]


def test_duplicate_schedule_sends_one_event_a_second_time_later(provider):
    plan = provider.plan("duplicate", duplicate_event_id="evt_0002")
    assert [d.event.id for d in plan] == ["evt_0001", "evt_0002", "evt_0003", "evt_0004", "evt_0002"]
    assert plan[1].note == "acknowledgement lost (simulated timeout)"
    assert plan[-1].note == "retry of #2"


def test_duplicate_schedule_picks_a_payment_from_the_seed(provider):
    first = provider.plan("duplicate")[-1].event
    assert first.type == "payment.succeeded"
    again = FakeProvider(SECRET, seed=1)
    for p in provider.payments.values():
        again.create_payment(p.customer, p.amount_cents)
    again.refund("pay_0001", 1_000)
    assert again.plan("duplicate")[-1].event.id == first.id  # same seed, same choice


def test_out_of_order_schedule_sends_everything_once_in_another_order(provider):
    plan = provider.plan("out_of_order")
    ids = [d.event.id for d in plan]
    assert sorted(ids) == [e.id for e in provider.events]
    assert ids != [e.id for e in provider.events]


def test_unknown_schedule_is_an_error(provider):
    with pytest.raises(ValueError):
        provider.plan("sometimes")

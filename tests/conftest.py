import pytest

from klaxon.patient.ledger import Ledger, WebhookEndpoint
from klaxon.patient.provider import FakeProvider

SECRET = b"test-secret-not-real"


@pytest.fixture
def provider() -> FakeProvider:
    """Three payments and one partial refund, fixed amounts so the evidence is easy to read."""
    p = FakeProvider(SECRET, seed=1)
    p.create_payment("cus_001", 2_500)  # pay_0001 $25.00 -> evt_0001
    p.create_payment("cus_002", 4_900)  # pay_0002 $49.00 -> evt_0002
    p.create_payment("cus_003", 1_250)  # pay_0003 $12.50 -> evt_0003
    p.refund("pay_0001", 1_000)  # $10.00 back      -> evt_0004
    return p


def make_endpoint(provider: FakeProvider, mode: str) -> tuple[Ledger, WebhookEndpoint]:
    ledger = Ledger()
    return ledger, WebhookEndpoint(ledger, SECRET, mode, now=provider.clock.now)

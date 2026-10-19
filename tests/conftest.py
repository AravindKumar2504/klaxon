import pytest

from klaxon.patient.ledger import Ledger, WebhookEndpoint
from klaxon.patient.provider import FakeProvider

SECRET = b"test-secret-not-real"
NO_CLAUDE = "/nonexistent/klaxon-offline-tests-never-call-claude"


@pytest.fixture(autouse=True)
def no_real_claude_code(monkeypatch):
    """No test can find the real Claude Code, whatever a mutant breaks: a live call needs Aravind's OK each time.
    KLAXON_CLAUDE_BIN wins over the PATH and the desktop app's copies, and a path that is not a file is refused,
    never passed over. A test that needs a `claude` points it at its own fake."""
    monkeypatch.setenv("KLAXON_CLAUDE_BIN", NO_CLAUDE)


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

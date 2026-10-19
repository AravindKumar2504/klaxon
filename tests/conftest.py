from pathlib import Path

import pytest

from klaxon.agent import claude_code
from klaxon.patient.ledger import Ledger, WebhookEndpoint
from klaxon.patient.provider import FakeProvider

SECRET = b"test-secret-not-real"
NO_CLAUDE = "/nonexistent/klaxon-offline-tests-never-call-claude"
NO_APP_COPIES = Path("/nonexistent/klaxon-offline-tests-see-no-app-copies")


@pytest.fixture(autouse=True)
def no_real_claude_code(monkeypatch):
    """The tests cannot find the real Claude Code: a live call needs Aravind's OK each time. Claude Code is found
    through KLAXON_CLAUDE_BIN, then the PATH, then the desktop app's copies. Here KLAXON_CLAUDE_BIN names a path that
    does not exist, which is refused, never passed over; and the app's copies are looked for in a folder that does
    not exist, so even code that skipped the first step (a mutant, say) finds none. The PATH stays: the mutation
    runner's is /usr/bin:/bin, which holds no claude. A test that needs a `claude` points KLAXON_CLAUDE_BIN at its
    own fake."""
    monkeypatch.setenv("KLAXON_CLAUDE_BIN", NO_CLAUDE)
    monkeypatch.setattr(claude_code, "APP_COPIES", NO_APP_COPIES)


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

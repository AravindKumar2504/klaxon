"""A demo of the approval gate, offline: a made-up stack in memory, and a diagnosis written by hand. No Docker,
no model, and nothing on this Mac's stack changes.

    make gate-demo          plans a rollback, prints its request id, and waits (10 minutes at most)
    make approve RUN=...    in a second terminal: shows the request; type its id to approve, anything else denies

Approved, the made-up ledger consumer is rolled back; denied or unanswered, nothing runs. Everything goes to a
temporary folder printed at the start: the request, the decision, the gate's log, and a deploy log of its own
(never ~/.klaxon/deploys.jsonl).

Run: python -m klaxon.demo.gate_demo
"""

from __future__ import annotations

import hashlib
import itertools
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from klaxon.agent import gate
from klaxon.agent.contract import Diagnosis, Evidence, FixAction, ProposedFix
from klaxon.agent.tools import clean_text
from klaxon.patient.deploys import SERVICES, Deployer, DeployLog

BANNER = (
    "SIMULATION: a made-up stack in memory and a diagnosis written by hand. No Docker, no model; this Mac's stack is"
    " not touched."
)
LEDGER = "ledger-consumer"
GOOD = {"LEDGER_HANDLER": "idempotent", "LEDGER_BAD_EVENTS": "dead_letter"}
NAIVE = GOOD | {"LEDGER_HANDLER": "naive"}
ALERT = {
    "name": "MoneyCheckFailed",
    "summary": "Made up for the demo: the money check failed 1 of its 5 invariants (posted_exactly_once).",
    "fired_at": None,
}


class MadeUpDocker:
    """One container per service, in memory: what klaxon.patient.deploys asks of docker, made up."""

    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.containers: dict[str, tuple[str, dict[str, str]]] = {}
        self.start(LEDGER, GOOD)

    def start(self, service: str, settings: dict[str, str]) -> None:
        environment = {inside: settings[name] for name, (inside, _) in SERVICES[service].items()}
        container = hashlib.sha256(f"made-up container {next(self.ids)}".encode()).hexdigest()
        self.containers[service] = (container, environment)

    def compose(self, *args: str, env: dict | None = None) -> str:
        if args[:2] == ("ps", "--quiet"):
            found = self.containers.get(args[2])
            return f"{found[0][:12]}\n" if found else ""
        if args[:4] == ("up", "--detach", "--no-deps", "--wait"):
            self.start(args[4], dict(env or {}))
            return ""
        raise ValueError(f"the made-up docker does not know: {args}")

    def inspect(self, *ids: str) -> list[dict]:
        for container, environment in self.containers.values():
            if container.startswith(ids[0]):
                return [{"Id": container, "Config": {"Env": [f"{k}={v}" for k, v in environment.items()]}}]
        return []


def made_up_diagnosis() -> Diagnosis:
    """Written by hand for the demo, in the shape the loop's diagnoses take. No model wrote it."""
    return Diagnosis(
        hypothesis=(
            "Made up for the demo: the ledger consumer was redeployed with LEDGER_HANDLER=naive, which books every"
            " webhook the provider sends again."
        ),
        evidence=(
            Evidence("c1", "money_check", {"wait": True}, "made up: events posted more than once"),
            Evidence("c2", "recent_deploys", {}, "made up: ledger-consumer redeployed with LEDGER_HANDLER=naive"),
        ),
        confidence=0.8,
        proposed_fix=ProposedFix("rollback", LEDGER, "Roll the ledger consumer back to its idempotent handler."),
        blast_radius="The made-up ledger consumer restarts.",
        rollback_plan="Deploy the naive handler again.",
        fix_action=FixAction("rollback_deploy", {"service": LEDGER}),
    )


def main(timeout: float = gate.WAIT_SECONDS, sleep: Callable[[float], None] = time.sleep) -> int:
    print(BANNER, flush=True)  # at once, even into a pipe: the demo then waits
    folder = Path(tempfile.mkdtemp(prefix="klaxon-gate-demo-"))
    docker = MadeUpDocker()
    deployer = Deployer(DeployLog(folder / "deploys.jsonl"), docker.compose, docker.inspect, ensure_local=lambda: None)
    deployer.deploy(LEDGER, NAIVE, by="the gate demo: a made-up bad deploy")
    print(f"Folder: {clean_text(str(folder))}", flush=True)
    print("The made-up ledger consumer runs LEDGER_HANDLER=naive, from a made-up bad deploy.\n", flush=True)
    outcome = gate.act(folder, made_up_diagnosis(), deployer, ALERT, timeout=timeout, sleep=sleep)
    _, environment = docker.containers[LEDGER]
    print(f"\nOutcome: {outcome.result}: {outcome.detail}")
    print(f"The made-up ledger consumer now runs LEDGER_HANDLER={environment['LEDGER_HANDLER']}.")
    print(f"The gate's log: {clean_text(str(folder / gate.LOG))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

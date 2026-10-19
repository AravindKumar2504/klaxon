"""A made-up docker for offline tests: one container per service, in memory. Not Docker, and labeled as such.

It answers the three things klaxon.patient.deploys asks of docker: `compose ps --quiet SERVICE`, `compose up
--detach --no-deps --wait SERVICE` (a new container, with the settings it is given), and `inspect ID`. Each
container's environment also holds a made-up password, so a test can show it never reaches a log.
"""

from __future__ import annotations

import hashlib
import itertools

from klaxon.patient.deploys import SERVICES, Deployer, DeployLog

NOW = 1_792_400_000.0  # 2026-10-19T08:53:20Z, a fixed time for the logs
PASSWORD = "made-up-password-not-real"
GOOD = {
    "ledger-consumer": {"LEDGER_HANDLER": "idempotent", "LEDGER_BAD_EVENTS": "dead_letter"},
    "payment-confirmer": {"CONFIRMER_HANDLER": "by_our_id", "CONFIRMER_BAD_EVENTS": "dead_letter"},
}
NAIVE = GOOD["ledger-consumer"] | {"LEDGER_HANDLER": "naive"}
RETRYING = GOOD["ledger-consumer"] | {"LEDGER_BAD_EVENTS": "retry"}


class FakeDocker:
    def __init__(self) -> None:
        self.ids = itertools.count(1)
        self.containers: dict[str, tuple[str, dict[str, str]]] = {}
        self.calls: list[tuple] = []
        self.local_checks = 0
        self.up_fails: Exception | None = None  # what `up` raises, when set
        self.takes = True  # False: `up` makes a new container that keeps its old settings
        for service, settings in GOOD.items():
            self.start(service, settings)

    def start(self, service: str, settings: dict[str, str]) -> str:
        """A container made outside any logged deploy (make up, or a recreate by hand): a new id, these settings. (A
        docker restart keeps a container's id; only a recreate makes a new one.)"""
        environment = {"DATABASE_URL": f"postgresql://writer:{PASSWORD}@postgres:5432/klaxon"}
        environment |= {inside: settings[name] for name, (inside, _) in SERVICES[service].items()}
        container = hashlib.sha256(f"container {next(self.ids)}".encode()).hexdigest()  # unlike prefixes too
        self.containers[service] = (container, environment)
        return container

    def settings(self, service: str) -> dict[str, str]:
        environment = self.containers[service][1]
        return {name: environment[inside] for name, (inside, _) in SERVICES[service].items()}

    def ups(self) -> list[tuple]:
        return [call for call in self.calls if call[0][0] == "up"]

    def compose(self, *args: str, env: dict | None = None) -> str:
        self.calls.append((args, None if env is None else dict(env)))
        if args[:2] == ("ps", "--quiet"):
            found = self.containers.get(args[2])
            return f"{found[0][:12]}\n" if found else ""
        if args[:4] == ("up", "--detach", "--no-deps", "--wait"):
            if self.up_fails is not None:
                raise self.up_fails
            service = args[4]
            self.start(service, dict(env) if self.takes else self.settings(service))
            return ""
        raise AssertionError(f"a compose call the made-up docker does not know: {args}")

    def inspect(self, *ids: str) -> list[dict]:
        self.calls.append((("inspect", *ids), None))
        for container, environment in self.containers.values():
            if container.startswith(ids[0]):
                return [{"Id": container, "Config": {"Env": [f"{k}={v}" for k, v in environment.items()]}}]
        return []

    def ensure_local(self) -> None:
        self.local_checks += 1

    def deployer(self, log: DeployLog | None = None) -> Deployer:
        return Deployer(log, self.compose, self.inspect, self.ensure_local, clock=lambda: NOW)

"""Deploys of this Mac's stack, and the log that remembers them: what a rollback goes back to.

A deploy here recreates one service's container with new behavior settings, the way the faults deploy a
known-bad setting, and waits until it is healthy. Every deploy is written to an append-only log: when, which
service, by whom, the container it made and its settings, and the container and settings it replaced. A rollback
(klaxon.agent.actions) reads a service's latest deploy from this log and goes back to the settings that deploy
replaced, much as `kubectl rollout undo` goes back to the previous revision. Only much as: a deploy here changes
behavior settings only, so a rollback restores those, not the image or the rest of the configuration, which are
those of the checkout that runs it.

Only behavior settings are kept: the few variables named in SERVICES, each with the values it may take. Never a
container's whole environment: it holds passwords. Some values are known to be bad (KNOWN_BAD: the faults' and the
demos' baselines); Klaxon's rollback never deploys one.

One deploy at a time: every deploy through a Deployer holds an exclusive lock (the log's .lock file) from its last
look at the running container to its line in the log, so a rollback that expects a container finds it, or does
nothing. A deploy made outside this code (docker compose by hand) takes no lock: the look right before acting is
then the only check.

The log is local state, not evidence: ~/.klaxon/deploys.jsonl, or the file KLAXON_DEPLOY_LOG names. It is shared
by every checkout of the repo, as the stack is. Each record names the container its deploy made, and a rollback
refuses unless that container is the one running. So a log older than the stack (after `make reset`) can never
steer a rollback, and deleting the log is safe: a rollback then finds nothing to go back to, and refuses.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from klaxon.patient import stack

# Per service: each compose variable a deploy sets, the name it has inside the container, and the values it may take.
SERVICES: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "ledger-consumer": {
        "LEDGER_HANDLER": ("LEDGER_HANDLER", ("idempotent", "naive")),
        "LEDGER_BAD_EVENTS": ("BAD_EVENTS", ("dead_letter", "retry")),
    },
    "payment-confirmer": {
        "CONFIRMER_HANDLER": ("CONFIRMER_HANDLER", ("by_our_id", "by_provider_id")),
        "CONFIRMER_BAD_EVENTS": ("BAD_EVENTS", ("dead_letter", "retry")),
    },
}
CONTAINER = re.compile(r"[0-9a-f]{64}")  # a container's full id, as `docker inspect` gives it
SHORT_ID = re.compile(r"[0-9a-f]{12,64}")  # as `docker compose ps --quiet` prints one
# The values known to be bad: the faults deploy them, and the demos use them as their known-bad baselines.
KNOWN_BAD = {
    "LEDGER_HANDLER": ("naive",),
    "LEDGER_BAD_EVENTS": ("retry",),
    "CONFIRMER_HANDLER": ("by_provider_id",),
    "CONFIRMER_BAD_EVENTS": ("retry",),
}
MAX_BY = 200  # characters of who or what made a deploy


class DeployError(RuntimeError):
    """A deploy that could not be made, read or recorded."""


class DeployChanged(DeployError):
    """A deploy that expected a container, its settings and the log's latest line, and found otherwise: not made."""


def known_bad(settings: Mapping[str, str]) -> list[str]:
    """The known-bad values among these settings, as NAME=value."""
    return [f"{name}={value}" for name, value in settings.items() if value in KNOWN_BAD.get(name, ())]


def default_log_path() -> Path:
    named = os.environ.get("KLAXON_DEPLOY_LOG")
    return Path(named) if named else Path.home() / ".klaxon" / "deploys.jsonl"


def check_settings(service: str, settings: Mapping[str, str]) -> dict[str, str]:
    """The settings, if they are exactly this service's variables, each with a value it may take."""
    known = SERVICES.get(service)
    if known is None:
        raise DeployError(f"no settings are known for the service {service!r}")
    if not isinstance(settings, Mapping) or set(settings) != set(known):
        raise DeployError(f"a deploy of {service} sets exactly {', '.join(sorted(known))}")
    for name, value in settings.items():
        if value not in known[name][1]:
            raise DeployError(f"{name} must be one of {', '.join(known[name][1])}")
    return {name: settings[name] for name in known}


def settings_of(service: str, environment: Mapping[str, str]) -> dict[str, str] | None:
    """The settings a container runs, read from its environment; None when one is missing or not a known value."""
    found = {name: environment.get(inside) for name, (inside, _) in SERVICES[service].items()}
    try:
        return check_settings(service, found)
    except DeployError:
        return None


def utc_now(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Running:
    """A service's running container: its full id, and its settings (None when they cannot be read)."""

    container: str
    settings: dict[str, str] | None


@dataclass(frozen=True)
class Deploy:
    """One line of the log."""

    seq: int
    at: str
    service: str
    by: str
    container: str
    settings: dict[str, str] | None
    replaced_container: str | None
    replaced_settings: dict[str, str] | None

    @classmethod
    def read(cls, line: str) -> Deploy:
        """A line of the log, checked: a line that is not well formed makes the whole log untrusted. A well-formed
        line written by hand is read like any other: the request shows what a rollback would undo."""
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != {field for field in cls.__dataclass_fields__}:
            raise ValueError("not a deploy record")
        seq, service = value["seq"], value["service"]
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 or service not in SERVICES:
            raise ValueError("an unknown service, or a bad sequence number")
        for name in ("at", "by"):
            if not isinstance(value[name], str) or not value[name]:
                raise ValueError(f"no {name}")
        if not isinstance(value["container"], str) or not CONTAINER.fullmatch(value["container"]):
            raise ValueError("not a container id")
        replaced = value["replaced_container"]
        if replaced is not None and (not isinstance(replaced, str) or not CONTAINER.fullmatch(replaced)):
            raise ValueError("not a container id")
        for name in ("settings", "replaced_settings"):
            if value[name] is not None:
                check_settings(service, value[name])
        return cls(**value)


class DeployLog:
    """The append-only log of deploys: one JSON object per line. With no path given, the one KLAXON_DEPLOY_LOG
    names when it is used (not when it is made), else ~/.klaxon/deploys.jsonl."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = None if path is None else Path(path)

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else default_log_path()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the deploy lock: one deploy at a time, from its last look at the stack to its line in the log."""
        lock = self.path.with_name(self.path.name + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        with open(lock, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def records(self) -> list[Deploy]:
        """Every deploy, oldest first. A line that cannot be read makes the whole log untrusted: DeployError."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        out = []
        for number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                out.append(Deploy.read(line))
            except (ValueError, TypeError, KeyError, DeployError) as exc:
                raise DeployError(f"the deploy log has a line it cannot read (line {number}): {exc}") from None
        return out

    def latest(self, service: str) -> Deploy | None:
        found = [record for record in self.records() if record.service == service]
        return found[-1] if found else None

    def append(self, **fields: object) -> Deploy:
        """Add one deploy, numbered after the last line. The file's own lock keeps two writers from sharing a
        number; the deploy lock (locked()) keeps two deploys from overlapping."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.seek(0)
            seq = sum(1 for line in handle if line.strip()) + 1  # counted, not parsed: a bad line never stops a deploy
            record = Deploy(seq=seq, **fields)
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record


class Deployer:
    """Deploys one service at a time on this Mac's stack, and logs each deploy. Tests pass their own parts."""

    def __init__(
        self,
        log: DeployLog | None = None,
        compose: Callable[..., str] = stack.compose,
        inspect: Callable[..., list[dict]] = stack.inspect,
        ensure_local: Callable[[], None] = stack.ensure_local,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.log = log or DeployLog()
        self._compose, self._inspect, self._ensure_local, self._clock = compose, inspect, ensure_local, clock

    def running(self, service: str) -> Running:
        """The service's one running container and its settings. Reads only."""
        if service not in SERVICES:
            raise DeployError(f"no settings are known for the service {service!r}")
        ids = (self._compose("ps", "--quiet", service) or "").split()
        if len(ids) != 1:
            raise DeployError(f"{service} has {len(ids)} running containers, not one")
        if not SHORT_ID.fullmatch(ids[0]):
            raise DeployError(f"docker gave {service} a container id that is not one")
        found = self._inspect(ids[0])
        if len(found) != 1 or not CONTAINER.fullmatch(str(found[0].get("Id", ""))):
            raise DeployError(f"docker could not describe {service}'s container")
        pairs = (found[0].get("Config") or {}).get("Env") or []
        environment = dict(pair.split("=", 1) for pair in pairs if isinstance(pair, str) and "=" in pair)
        return Running(found[0]["Id"], settings_of(service, environment))

    def check_local(self) -> None:
        """Docker must talk to this Mac, never to another host: checked before docker is read or changed."""
        self._ensure_local()

    def deploy(self, service: str, settings: Mapping[str, str], by: str, expect: Expect | None = None) -> Deploy | None:
        """Recreate the service's container with these settings, wait until it is healthy, and log the deploy, all
        under the deploy lock. With expect, only if the service still runs that container with those settings and
        the log's latest line for it is that one: else DeployChanged, and nothing is done. Nothing is done, and
        nothing logged, when it already runs the settings: None. A service that is not running (or cannot be read)
        is deployed all the same, as `compose up` would: a fault's clearing must not depend on it. The log then says
        that what it replaced is not known, and no rollback will go back to it."""
        wanted = check_settings(service, settings)
        self.check_local()
        with self.log.locked():
            try:
                before: Running | None = self.running(service)
            except DeployError:
                before = None
            if expect is not None:
                latest = self.log.latest(service)
                if (
                    before is None
                    or before.container != expect.container
                    or before.settings != expect.settings
                    or latest is None
                    or latest.seq != expect.seq
                ):
                    raise DeployChanged(f"{service} changed since it was looked at: nothing was deployed")
            if before is not None and before.settings == wanted:
                return None
            self._compose("up", "--detach", "--no-deps", "--wait", service, env=wanted)
            after = self.running(service)
            record = self.log.append(
                at=utc_now(self._clock),
                service=service,
                by=" ".join(by.split())[:MAX_BY] or "unknown",
                container=after.container,
                settings=after.settings,
                replaced_container=None if before is None else before.container,
                replaced_settings=None if before is None else before.settings,
            )
        if after.settings != wanted:  # logged as it happened, then said
            raise DeployError(f"{service} was redeployed, but its new container does not run the settings asked for")
        return record


@dataclass(frozen=True)
class Expect:
    """What a deploy expects to find: the running container, its settings, and the log's latest line for it."""

    container: str
    settings: dict[str, str]
    seq: int

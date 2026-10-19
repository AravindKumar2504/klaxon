"""Klaxon's write actions: planned from the live state, and run only after a person's approval (klaxon.agent.gate).

plan() only reads. For rollback_deploy, it reads the service's latest deploy from the deploy log
(klaxon.patient.deploys) and the container that is running, and refuses unless all of these hold:

    the deploy log has a deploy of the service, and knows the settings that deploy replaced;
    the running container is the one that deploy made (not one recreated outside the log since);
    it still runs the settings that deploy gave it;
    going back changes something, and deploys no known-bad setting (deploys.KNOWN_BAD).

The last one matters after a fault was cleared: the latest deploy then replaced the fault's bad setting, and
"going back" would deploy it again. Klaxon never does; a person can, by hand.

The plan says the exact change, which deploy it undoes, and how to undo it in turn: roll back again, unless that
would deploy a known-bad setting, which only a person may do.

run() acts, and only on a plan a person approved: the gate is its one caller. A request can wait ten minutes, so
the deploy itself checks, under the deploy lock and right before it acts, that what the plan was made on still
holds: the same latest deploy, the same container, the same settings. If anything changed, it refuses and
changes nothing. A change made outside Klaxon's code, in the moment between that check and the deploy, is not
stopped (deploys.py says why). The rollback is itself a deploy, logged with the request that approved it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from klaxon.agent.catalog import CATALOG
from klaxon.agent.contract import check
from klaxon.agent.tools import clean_text
from klaxon.patient.deploys import Deploy, DeployChanged, Deployer, DeployError, Expect, known_bad
from klaxon.patient.stack import StackError


class ActionRefused(RuntimeError):
    """An action that must not run, and why. Nothing was changed."""


@dataclass(frozen=True)
class Plan:
    """What an approved action will do, worked out by code from the live state, before anyone is asked."""

    action: str
    arguments: dict
    service: str
    container: str  # the running container it was planned on: it must still be the one running when it runs
    from_settings: dict[str, str]
    to_settings: dict[str, str]
    undoes: dict  # the deploy it undoes: its number, time, author and container
    change: str  # the exact change, in one line
    undo: str  # how to undo it, in one line

    def as_dict(self) -> dict:
        return asdict(self)


def plan(name: str, arguments: object, deployer: Deployer) -> Plan:
    """The plan of a catalog action, with these arguments, on the stack as it is now. Reads only."""
    spec = CATALOG.get(name) if isinstance(name, str) else None
    if spec is None:
        raise ActionRefused(f"{clean_text(str(name), 64)} is not an action in the catalog")
    problems = check(spec.input_schema, arguments, "arguments")
    if problems:
        raise ActionRefused("; ".join(problems))
    try:
        deployer.check_local()  # docker must talk to this Mac before it is read
        return PLANNERS[spec.name](arguments, deployer)
    except (DeployError, StackError) as exc:
        raise ActionRefused(f"the stack could not be read: {clean_text(str(exc), 300)}") from None


def _plan_rollback(arguments: dict, deployer: Deployer) -> Plan:
    service = arguments["service"]
    latest = deployer.log.latest(service)
    if latest is None:
        raise ActionRefused(f"the deploy log has no deploy of {service}: there is nothing to roll back to")
    if latest.replaced_settings is None:
        raise ActionRefused(f"the deploy log does not know the settings {service} ran before its latest deploy")
    now = deployer.running(service)
    if now.container != latest.container:
        raise ActionRefused(
            f"{service}'s running container is not the one its latest logged deploy made: it was recreated"
            " outside the log since"
        )
    if now.settings is None or now.settings != latest.settings:
        raise ActionRefused(f"{service} no longer runs the settings its latest logged deploy gave it")
    target = latest.replaced_settings
    if target == now.settings:
        raise ActionRefused(f"{service}'s latest deploy changed none of its settings: there is nothing to roll back")
    bad = known_bad(target)
    if bad:
        raise ActionRefused(
            f"rolling {service} back would deploy a known-bad setting ({', '.join(bad)}): Klaxon never deploys one;"
            " a person can, by hand"
        )
    change = f"redeploy {service}: " + ", ".join(
        f"{name} {now.settings[name]} -> {value}" if now.settings[name] != value else f"{name} {value} (unchanged)"
        for name, value in target.items()
    )
    back = ", ".join(f"{name}={now.settings[name]}" for name, value in target.items() if now.settings[name] != value)
    undo = f"rollback_deploy {service} again: it would go back to {back}"
    if known_bad(now.settings):  # undoing it would deploy what it removes: a person's call, never Klaxon's
        undo = f"going back to {back} would deploy a known-bad setting: Klaxon will not; a person can, by hand"
    return Plan(
        action="rollback_deploy",
        arguments={"service": service},
        service=service,
        container=now.container,
        from_settings=dict(now.settings),
        to_settings=dict(target),
        undoes={"seq": latest.seq, "at": latest.at, "by": latest.by, "container": latest.container[:12]},
        change=change,
        undo=undo,
    )


PLANNERS = {"rollback_deploy": _plan_rollback}  # one per catalog action (a test checks)


def run(approved: Plan, deployer: Deployer, by: str) -> Deploy:
    """Run a plan a person approved, only if what it was made on still holds, as the deploy checks under its lock.
    by: who approved it, for the log."""
    expect = Expect(approved.container, dict(approved.from_settings), approved.undoes["seq"])
    try:
        record = deployer.deploy(approved.service, approved.to_settings, by=by, expect=expect)
    except DeployChanged:
        raise ActionRefused(f"{approved.service} changed since the request was made: nothing was done") from None
    if record is None:  # it already runs them: the expected settings make this impossible, but never claim a deploy
        raise ActionRefused(f"{approved.service} already runs those settings: nothing was done")
    return record

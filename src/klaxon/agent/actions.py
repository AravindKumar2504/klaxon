"""Klaxon's write actions: planned from the live state, and run only after a person's approval (klaxon.agent.gate).

plan() only reads. For rollback_deploy, it reads the service's latest deploy from the deploy log
(klaxon.patient.deploys) and the container that is running, and refuses unless all of these hold:

    the deploy log has a deploy of the service, and knows the settings that deploy replaced;
    the running container is the one that deploy made (not one restarted or redeployed outside the log since);
    it still runs the settings that deploy gave it;
    going back changes something.

The plan says the exact change, which deploy it undoes, and how to undo it in turn: roll back again.

run() acts, and only on a plan a person approved: the gate is its one caller. A request can wait ten minutes, so
run() first checks again that what the plan was made on still holds: the same latest deploy, the same container,
the same settings. If anything changed, it refuses and changes nothing. The rollback is itself a deploy, logged
with the request that approved it, so the next rollback would undo it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from klaxon.agent.catalog import CATALOG
from klaxon.agent.contract import check
from klaxon.agent.tools import clean_text
from klaxon.patient.deploys import Deploy, Deployer, DeployError
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
            f"{service}'s running container is not the one its latest logged deploy made: it was restarted or"
            " redeployed outside the log since"
        )
    if now.settings is None or now.settings != latest.settings:
        raise ActionRefused(f"{service} no longer runs the settings its latest logged deploy gave it")
    target = latest.replaced_settings
    if target == now.settings:
        raise ActionRefused(f"{service}'s latest deploy changed none of its settings: there is nothing to roll back")
    change = f"redeploy {service}: " + ", ".join(
        f"{name} {now.settings[name]} -> {value}" if now.settings[name] != value else f"{name} {value} (unchanged)"
        for name, value in target.items()
    )
    back = ", ".join(f"{name}={now.settings[name]}" for name, value in target.items() if now.settings[name] != value)
    return Plan(
        action="rollback_deploy",
        arguments={"service": service},
        service=service,
        container=now.container,
        from_settings=dict(now.settings),
        to_settings=dict(target),
        undoes={"seq": latest.seq, "at": latest.at, "by": latest.by, "container": latest.container[:12]},
        change=change,
        undo=f"rollback_deploy {service} again: it would go back to {back}",
    )


PLANNERS = {"rollback_deploy": _plan_rollback}  # one per catalog action (a test checks)


def run(approved: Plan, deployer: Deployer, by: str) -> Deploy:
    """Run a plan a person approved, only if what it was made on still holds. by: who approved it, for the log."""
    try:
        latest = deployer.log.latest(approved.service)
        now = deployer.running(approved.service)
    except (DeployError, StackError) as exc:
        raise ActionRefused(f"the stack could not be read again: {clean_text(str(exc), 300)}") from None
    if (
        latest is None
        or latest.seq != approved.undoes["seq"]
        or now.container != approved.container
        or now.settings != approved.from_settings
    ):
        raise ActionRefused(f"{approved.service} changed since the request was made: nothing was done")
    record = deployer.deploy(approved.service, approved.to_settings, by=by)
    if record is None:  # it already runs them: the checks above make this impossible, but never claim a deploy
        raise ActionRefused(f"{approved.service} already runs those settings: nothing was done")
    return record

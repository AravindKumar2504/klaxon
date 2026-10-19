"""Klaxon's write actions, offline, on a made-up docker (tests/fake_docker.py): the catalog, how a rollback is
planned from the deploy log and the running container, what makes it refuse, and the check again before it runs.
Nothing here is approved by a person: these call plan() and run() directly. The gate's tests show who may."""

import json

import pytest

from klaxon.agent import actions, catalog
from klaxon.agent.actions import ActionRefused
from klaxon.agent.contract import ANNOTATIONS, KEYWORDS
from klaxon.agent.tools import HOME
from klaxon.patient.deploys import SERVICES, DeployLog
from klaxon.patient.stack import StackError
from tests.fake_docker import GOOD, NAIVE, RETRYING, FakeDocker

LEDGER = "ledger-consumer"


def after_a_bad_deploy(settings=NAIVE):
    docker = FakeDocker()
    deployer = docker.deployer()
    deployer.deploy(LEDGER, settings, by="klaxon.faults")
    return docker, deployer


def test_the_catalog_holds_one_action_and_each_has_a_planner():
    assert list(catalog.CATALOG) == ["rollback_deploy"]  # Aravind's choice, 2026-10-19
    assert set(actions.PLANNERS) == set(catalog.CATALOG)


def test_the_services_a_rollback_may_name_are_those_the_deploy_log_knows():
    assert set(catalog.ROLLBACK_SERVICES) == set(SERVICES)
    schema = catalog.ROLLBACK_DEPLOY.input_schema
    assert schema["properties"]["service"]["enum"] == list(catalog.ROLLBACK_SERVICES)


def test_the_catalog_is_data_its_schemas_use_only_what_the_validator_reads():
    def keywords(schema):
        found = set(schema)
        for value in schema.get("properties", {}).values():
            found |= keywords(value)
        return found

    for spec in catalog.CATALOG.values():
        assert keywords(spec.input_schema) <= KEYWORDS | ANNOTATIONS
        assert json.loads(json.dumps(spec.input_schema)) == spec.input_schema


def test_a_rollback_plans_the_exact_change_back_to_what_the_latest_deploy_replaced():
    docker, deployer = after_a_bad_deploy()
    ups = len(docker.ups())
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    assert len(docker.ups()) == ups  # planning only reads
    assert plan.change == (
        "redeploy ledger-consumer: LEDGER_HANDLER naive -> idempotent, LEDGER_BAD_EVENTS dead_letter (unchanged)"
    )
    assert plan.undo == "rollback_deploy ledger-consumer again: it would go back to LEDGER_HANDLER=naive"
    assert (plan.from_settings, plan.to_settings) == (NAIVE, GOOD[LEDGER])
    assert plan.container == docker.containers[LEDGER][0]
    assert plan.undoes == {
        "seq": 1,
        "at": "2026-10-19T08:53:20Z",
        "by": "klaxon.faults",
        "container": plan.container[:12],
    }


def test_a_rollback_of_the_poison_deploy_goes_back_to_dead_lettering():
    _, deployer = after_a_bad_deploy(RETRYING)
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    assert "LEDGER_BAD_EVENTS retry -> dead_letter" in plan.change and plan.to_settings == GOOD[LEDGER]


@pytest.mark.parametrize(
    ("name", "arguments", "problem"),
    [
        ("restart_service", {"service": LEDGER}, "restart_service is not an action in the catalog"),
        (None, {}, "None is not an action in the catalog"),
        ("rollback_deploy", {}, "arguments.service: is required"),
        ("rollback_deploy", {"service": "postgres"}, "arguments.service: must be one of"),
        ("rollback_deploy", {"service": LEDGER, "to": "naive"}, "arguments.to: is not a field here"),
        ("rollback_deploy", [LEDGER], "arguments: must be a JSON object"),
    ],
)
def test_an_action_off_the_catalog_or_with_bad_arguments_is_refused(name, arguments, problem):
    docker, deployer = after_a_bad_deploy()
    with pytest.raises(ActionRefused, match=problem):
        actions.plan(name, arguments, deployer)


def test_with_no_deploy_logged_there_is_nothing_to_roll_back_to():
    docker = FakeDocker()
    with pytest.raises(ActionRefused, match="the deploy log has no deploy of ledger-consumer"):
        actions.plan("rollback_deploy", {"service": LEDGER}, docker.deployer())


def test_a_container_restarted_or_redeployed_outside_the_log_is_never_rolled_back():
    """make reset and make up, say: the log then describes containers that are gone."""
    docker, deployer = after_a_bad_deploy()
    docker.start(LEDGER, NAIVE)  # the same settings, but not the container the logged deploy made
    with pytest.raises(ActionRefused, match="not the one its latest logged deploy made"):
        actions.plan("rollback_deploy", {"service": LEDGER}, deployer)


def test_a_service_whose_settings_changed_since_its_logged_deploy_is_refused():
    docker, deployer = after_a_bad_deploy()
    docker.containers[LEDGER][1]["LEDGER_HANDLER"] = "idempotent"  # changed in place, outside the log
    with pytest.raises(ActionRefused, match="no longer runs the settings its latest logged deploy gave it"):
        actions.plan("rollback_deploy", {"service": LEDGER}, deployer)


def test_settings_before_the_deploy_that_are_not_known_are_never_guessed():
    docker = FakeDocker()
    docker.containers[LEDGER][1]["BAD_EVENTS"] = "something-else"
    deployer = docker.deployer()
    deployer.deploy(LEDGER, NAIVE, by="klaxon.faults")
    with pytest.raises(ActionRefused, match="does not know the settings ledger-consumer ran before"):
        actions.plan("rollback_deploy", {"service": LEDGER}, deployer)


def test_a_logged_deploy_that_changed_nothing_has_nothing_to_roll_back():
    docker = FakeDocker()
    container = docker.containers[LEDGER][0]
    DeployLog().append(
        at="2026-10-19T08:53:20Z",
        service=LEDGER,
        by="by hand",
        container=container,
        settings=GOOD[LEDGER],
        replaced_container=container,
        replaced_settings=GOOD[LEDGER],
    )
    with pytest.raises(ActionRefused, match="changed none of its settings"):
        actions.plan("rollback_deploy", {"service": LEDGER}, docker.deployer())


def test_a_stack_that_cannot_be_read_is_refused_without_its_paths():
    docker, deployer = after_a_bad_deploy()

    def broken(*args, env=None):
        raise StackError(f"docker compose --file {HOME}/klaxon/compose.yaml failed")

    docker.compose = broken
    with pytest.raises(ActionRefused, match=r"the stack could not be read: docker compose --file ~/klaxon"):
        actions.plan("rollback_deploy", {"service": LEDGER}, docker.deployer())


def test_a_log_that_cannot_be_trusted_is_refused():
    docker, deployer = after_a_bad_deploy()
    with open(DeployLog().path, "a") as handle:
        handle.write("{edited by hand\n")
    with pytest.raises(ActionRefused, match="a line it cannot read"):
        actions.plan("rollback_deploy", {"service": LEDGER}, deployer)


def test_an_approved_rollback_deploys_and_is_logged_with_its_approval_so_the_next_rollback_undoes_it():
    docker, deployer = after_a_bad_deploy()
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    record = actions.run(plan, deployer, by="Klaxon, approved request r-0a1b")
    assert docker.settings(LEDGER) == GOOD[LEDGER]
    assert (record.by, record.settings, record.replaced_settings) == (
        "Klaxon, approved request r-0a1b",
        GOOD[LEDGER],
        NAIVE,
    )
    undo = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)  # its undo: roll back again
    assert undo.to_settings == NAIVE and undo.undoes["by"] == "Klaxon, approved request r-0a1b"


@pytest.mark.parametrize("change", ["container", "settings", "a new deploy", "a new line in the log"])
def test_what_changed_while_the_request_waited_is_never_rolled_back_over(change):
    docker, deployer = after_a_bad_deploy()
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    if change == "container":
        docker.start(LEDGER, NAIVE)
    elif change == "settings":
        docker.containers[LEDGER][1]["LEDGER_HANDLER"] = "idempotent"
    elif change == "a new deploy":
        deployer.deploy(LEDGER, RETRYING | {"LEDGER_HANDLER": "naive"}, by="someone else")
    else:  # the same container and settings, but the log moved on: what the plan undoes is no longer the latest
        latest = DeployLog().latest(LEDGER)
        DeployLog().append(
            at=latest.at,
            service=LEDGER,
            by="by hand",
            container=latest.container,
            settings=latest.settings,
            replaced_container=latest.container,
            replaced_settings=GOOD[LEDGER],
        )
    ups = len(docker.ups())
    with pytest.raises(ActionRefused, match="changed since the request was made: nothing was done"):
        actions.run(plan, deployer, by="Klaxon, approved request r-0a1b")
    assert len(docker.ups()) == ups


def test_a_rollback_that_fails_in_docker_says_so_and_logs_no_deploy():
    docker, deployer = after_a_bad_deploy()
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    docker.up_fails = StackError("the container did not turn healthy")
    with pytest.raises(StackError):
        actions.run(plan, deployer, by="Klaxon, approved request r-0a1b")
    assert len(DeployLog().records()) == 1


def test_running_a_plan_checks_that_docker_points_at_this_mac():
    docker, deployer = after_a_bad_deploy()
    plan = actions.plan("rollback_deploy", {"service": LEDGER}, deployer)
    checks = docker.local_checks
    actions.run(plan, deployer, by="Klaxon, approved request r-0a1b")
    assert docker.local_checks == checks + 1


def test_a_deploy_of_a_stopped_service_is_never_rolled_back_to_a_guess():
    docker = FakeDocker()
    del docker.containers[LEDGER]
    deployer = docker.deployer()
    deployer.deploy(LEDGER, NAIVE, by="klaxon.faults")
    with pytest.raises(ActionRefused, match="does not know the settings ledger-consumer ran before"):
        actions.plan("rollback_deploy", {"service": LEDGER}, deployer)

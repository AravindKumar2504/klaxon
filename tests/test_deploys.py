"""Deploys and the deploy log, offline, on a made-up docker (tests/fake_docker.py): what a deploy changes, what
the log keeps, and what it refuses to trust."""

import json
import threading
from pathlib import Path

import pytest

from klaxon.patient import deploys
from klaxon.patient.deploys import SERVICES, Deploy, DeployError, DeployLog, check_settings, settings_of
from tests.fake_docker import GOOD, NAIVE, PASSWORD, RETRYING, FakeDocker


def test_a_deploy_recreates_one_service_on_this_mac_and_logs_what_it_made_and_what_it_replaced():
    docker = FakeDocker()
    old = docker.containers["ledger-consumer"][0]
    record = docker.deployer().deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    assert docker.local_checks == 1  # docker is checked to point at this Mac before anything is read or changed
    assert docker.ups() == [(("up", "--detach", "--no-deps", "--wait", "ledger-consumer"), NAIVE)]
    assert docker.settings("ledger-consumer") == NAIVE
    assert (record.seq, record.service, record.by) == (1, "ledger-consumer", "klaxon.faults")
    assert record.container == docker.containers["ledger-consumer"][0] != old
    assert (record.settings, record.replaced_container, record.replaced_settings) == (
        NAIVE,
        old,
        GOOD["ledger-consumer"],
    )
    assert record.at == "2026-10-19T08:53:20Z"
    assert DeployLog().records() == [record]  # the log this test was given (conftest), read back


def test_the_log_keeps_the_behavior_settings_and_never_a_containers_environment():
    docker = FakeDocker()
    docker.deployer().deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    text = DeployLog().path.read_text()
    assert PASSWORD not in text and "DATABASE_URL" not in text
    assert set(json.loads(text)) == {
        "seq",
        "at",
        "service",
        "by",
        "container",
        "settings",
        "replaced_container",
        "replaced_settings",
    }


def test_a_deploy_of_the_settings_a_service_already_runs_does_nothing_and_logs_nothing():
    docker = FakeDocker()
    assert docker.deployer().deploy("ledger-consumer", GOOD["ledger-consumer"], by="klaxon.faults") is None
    assert docker.ups() == [] and DeployLog().records() == []


@pytest.mark.parametrize(
    ("service", "settings", "problem"),
    [
        ("redis", {}, "no settings are known for the service 'redis'"),
        ("ledger-consumer", {"LEDGER_HANDLER": "naive"}, "sets exactly LEDGER_BAD_EVENTS, LEDGER_HANDLER"),
        ("ledger-consumer", NAIVE | {"DATABASE_URL": "x"}, "sets exactly"),
        (
            "ledger-consumer",
            NAIVE | {"LEDGER_HANDLER": "naive; rm -rf /"},
            "LEDGER_HANDLER must be one of idempotent, naive",
        ),
        ("ledger-consumer", GOOD["payment-confirmer"], "sets exactly"),
        ("ledger-consumer", ["LEDGER_HANDLER"], "sets exactly"),
    ],
)
def test_a_deploy_sets_exactly_its_services_settings_each_to_a_value_it_may_take(service, settings, problem):
    docker = FakeDocker()
    with pytest.raises(DeployError, match=problem.replace(";", ".")):
        docker.deployer().deploy(service, settings, by="test")
    assert docker.ups() == [] and docker.local_checks == 0  # refused before docker is touched


def test_settings_are_read_back_from_the_names_they_have_inside_the_container():
    assert settings_of("ledger-consumer", {"LEDGER_HANDLER": "naive", "BAD_EVENTS": "retry"}) == {
        "LEDGER_HANDLER": "naive",
        "LEDGER_BAD_EVENTS": "retry",
    }
    assert settings_of("ledger-consumer", {"LEDGER_HANDLER": "naive"}) is None  # one missing: not known
    assert settings_of("ledger-consumer", {"LEDGER_HANDLER": "other", "BAD_EVENTS": "retry"}) is None
    assert check_settings("payment-confirmer", GOOD["payment-confirmer"]) == GOOD["payment-confirmer"]


def test_a_service_must_have_exactly_one_running_container_that_docker_can_describe():
    docker = FakeDocker()
    deployer = docker.deployer()
    del docker.containers["ledger-consumer"]
    with pytest.raises(DeployError, match="has 0 running containers, not one"):
        deployer.running("ledger-consumer")
    docker.compose = lambda *args, env=None: "aaaaaaaaaaaa\nbbbbbbbbbbbb\n"
    with pytest.raises(DeployError, match="has 2 running containers"):
        docker.deployer().running("ledger-consumer")
    docker.compose = lambda *args, env=None: "--format=x\n"
    with pytest.raises(DeployError, match="not one"):
        docker.deployer().running("ledger-consumer")
    docker.compose = lambda *args, env=None: "aaaaaaaaaaaa\n"
    docker.inspect = lambda *ids: [{"Id": "not-an-id"}]
    with pytest.raises(DeployError, match="could not describe"):
        docker.deployer().running("ledger-consumer")
    with pytest.raises(DeployError, match="no settings are known"):
        deployer.running("postgres")


def test_a_deploy_that_does_not_take_is_logged_as_it_happened_then_said():
    docker = FakeDocker()
    docker.takes = False
    with pytest.raises(DeployError, match="does not run the settings asked for"):
        docker.deployer().deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    (record,) = DeployLog().records()
    assert record.settings == GOOD["ledger-consumer"] and record.replaced_settings == GOOD["ledger-consumer"]


def test_a_deploy_that_fails_logs_nothing():
    docker = FakeDocker()
    docker.up_fails = deploys.stack.StackError("docker compose up failed")
    with pytest.raises(deploys.stack.StackError):
        docker.deployer().deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    assert DeployLog().records() == []


def test_the_latest_deploy_is_the_last_one_of_that_service():
    docker = FakeDocker()
    deployer = docker.deployer()
    first = deployer.deploy("ledger-consumer", NAIVE, by="one")
    other = deployer.deploy(
        "payment-confirmer", GOOD["payment-confirmer"] | {"CONFIRMER_HANDLER": "by_provider_id"}, by="two"
    )
    second = deployer.deploy("ledger-consumer", RETRYING, by="three")
    assert [r.seq for r in DeployLog().records()] == [1, 2, 3]
    assert DeployLog().latest("ledger-consumer") == second and second.replaced_container == first.container
    assert DeployLog().latest("payment-confirmer") == other
    assert DeployLog(Path("/nonexistent/klaxon/deploys.jsonl")).latest("ledger-consumer") is None


def _line(**changes):
    record = {
        "seq": 1,
        "at": "2026-10-19T02:30:50Z",
        "service": "ledger-consumer",
        "by": "klaxon.faults",
        "container": "a" * 64,
        "settings": NAIVE,
        "replaced_container": "b" * 64,
        "replaced_settings": GOOD["ledger-consumer"],
    }
    return json.dumps(record | changes)


@pytest.mark.parametrize(
    "line",
    [
        "{not json",
        "[1, 2]",
        _line(seq=True),
        _line(seq=0),
        _line(service="postgres"),
        _line(container="a" * 12),
        _line(container="A" * 64),
        _line(replaced_container="x"),
        _line(settings={"LEDGER_HANDLER": "naive"}),
        _line(replaced_settings=NAIVE | {"LEDGER_BAD_EVENTS": "drop"}),
        _line(by=""),
        _line(extra="field"),
        json.dumps({key: value for key, value in json.loads(_line()).items() if key != "by"}),
    ],
)
def test_a_line_that_was_edited_by_hand_makes_the_whole_log_untrusted(line):
    log = DeployLog()
    log.path.write_text(_line() + "\n" + line + "\n")
    with pytest.raises(DeployError, match=r"the deploy log has a line it cannot read \(line 2\)"):
        log.latest("ledger-consumer")


def test_a_bad_line_never_stops_a_deploy_from_being_logged():
    log = DeployLog()
    log.path.write_text("{not json\n\n")
    docker = FakeDocker()
    record = docker.deployer(log).deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    assert record.seq == 2  # numbered after the lines there, read or not
    assert Deploy.read(log.path.read_text().splitlines()[-1]) == record


def test_unknown_settings_in_a_running_container_are_logged_as_unknown_never_guessed():
    docker = FakeDocker()
    docker.containers["ledger-consumer"][1]["BAD_EVENTS"] = "something-else"
    record = docker.deployer().deploy("ledger-consumer", NAIVE, by="klaxon.faults")
    assert record.replaced_settings is None and record.settings == NAIVE


def test_who_made_a_deploy_is_one_short_line():
    docker = FakeDocker()
    record = docker.deployer().deploy("ledger-consumer", NAIVE, by="a\nb " + "c" * 500)
    assert "\n" not in record.by and len(record.by) == deploys.MAX_BY


def test_two_processes_deploying_at_once_never_share_a_number():
    log = DeployLog()
    fields = json.loads(_line())
    del fields["seq"]

    def append_many():
        for _ in range(25):
            log.append(**fields)

    threads = [threading.Thread(target=append_many) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.seq for r in log.records()) == list(range(1, 101))


def test_the_log_lives_where_klaxon_deploy_log_says_else_in_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("KLAXON_DEPLOY_LOG", str(tmp_path / "x.jsonl"))
    assert DeployLog().path == tmp_path / "x.jsonl"
    monkeypatch.delenv("KLAXON_DEPLOY_LOG")
    assert deploys.default_log_path() == Path.home() / ".klaxon" / "deploys.jsonl"


def test_the_settings_known_are_those_compose_yaml_lets_a_deploy_set():
    compose = (deploys.stack.COMPOSE_FILE).read_text()
    for service, variables in SERVICES.items():
        for name, (inside, values) in variables.items():
            assert f"{inside}: ${{{name}:-{values[0]}}}" in compose, (service, name)

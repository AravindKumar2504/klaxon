"""The webhook receiver in Go (week 8, item 2): how the repo builds, runs and checks it.

Offline: these read the files. The Go code's own tests run with make test-go, in the official golang image; the
shared table of cases is run against the Python receiver in tests/services/test_receiver_shared_cases.py.
"""

import re

import yaml

from klaxon.patient.stack import REPO

COMPOSE = yaml.safe_load((REPO / "compose.yaml").read_text())
DOCKERFILE = (REPO / "docker" / "receiver.Dockerfile").read_text()
MAKEFILE = (REPO / "Makefile").read_text()
CI = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text())
PINNED = re.compile(r"golang:(\d+\.\d+\.\d+)-alpine@sha256:[0-9a-f]{64}")


def test_compose_runs_the_go_receiver_from_its_own_image_and_its_binary_checks_its_health():
    receiver = COMPOSE["services"]["webhook-receiver"]
    assert receiver["image"] == "klaxon-receiver:dev"
    assert receiver["build"] == {"context": ".", "dockerfile": "docker/receiver.Dockerfile"}
    assert "command" not in receiver and "entrypoint" not in receiver  # the image's: /receiver
    assert receiver["healthcheck"]["test"] == ["CMD", "/receiver", "-health"]  # no shell or Python in the image
    others = {
        name: s["healthcheck"] for name, s in COMPOSE["services"].items() if s.get("image") == "klaxon-patient:dev"
    }
    for timing in ("interval", "timeout", "retries", "start_period", "start_interval"):
        assert receiver["healthcheck"][timing] == others["payments-api"][timing]  # the same timings as before
    assert receiver["init"] is True and receiver["restart"] == "unless-stopped"


def test_the_image_is_built_in_the_pinned_golang_image_the_tests_run_in_and_runs_from_scratch():
    built_in = PINNED.search(DOCKERFILE)
    tested_in = re.search(r"^GO_IMAGE := (\S+)$", MAKEFILE, re.MULTILINE)
    assert built_in and tested_in and built_in.group(0) == tested_in.group(1)
    stages = re.findall(r"^FROM (\S+)", DOCKERFILE, re.MULTILINE)
    assert stages == [built_in.group(0), "scratch"]
    assert "ENV CGO_ENABLED=0" in DOCKERFILE  # a static binary: scratch has no C library
    assert re.search(r"^USER 10001:10001$", DOCKERFILE, re.MULTILINE)  # not root, as the Python image's user
    assert re.search(r'^ENTRYPOINT \["/receiver"\]$', DOCKERFILE, re.MULTILINE)


def test_make_test_go_checks_format_vet_and_tests_on_a_read_only_copy():
    target = MAKEFILE.split("\ntest-go:")[1].split("\n\n")[0]
    assert "local-docker-only" in target.splitlines()[0]
    assert 'source="$(CURDIR)/go/receiver",target=/src,readonly' in target  # nothing written to the repo
    assert "gofmt -l ." in target and "go vet ./..." in target and "go test -count=1 ./..." in target
    assert "$(GO_IMAGE)" in target


def test_ci_runs_make_test_go_with_its_rules():
    job = CI["jobs"]["go"]
    steps = job["steps"]
    assert any(step.get("run") == "make test-go" for step in steps)
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"])  # pinned to a full commit hash
            assert step.get("with", {}).get("persist-credentials") is False
    assert CI["permissions"] == {"contents": "read"} and "permissions" not in job

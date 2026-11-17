"""The venv's pins (week 7, item 3; Aravind's choice: "Pin them now", 2026-11-17).

Every library in this project's venv is pinned, in requirements/graph.lock.txt (LangGraph's, week 6) or in
requirements/dev.lock.txt (the test tools and the services' libraries), and the venv runs exactly those versions. So a
venv made by make venv on this Mac, in CI, or by make ci-linux holds the same libraries at the same versions."""

import re
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(lock: str) -> dict[str, str]:
    lines = [line.split("#")[0].strip() for line in (REPO / "requirements" / lock).read_text().splitlines()]
    pinned = [line.split("==") for line in lines if line]
    assert all(len(pair) == 2 and pair[1] for pair in pinned), lock  # exact versions only, never a range
    return {norm(name): version for name, version in pinned}


def test_the_two_locks_pin_different_libraries_each_exactly():
    graph, dev = pins("graph.lock.txt"), pins("dev.lock.txt")
    assert len(dev) == 45 and not set(graph) & set(dev)


def test_the_venv_runs_every_pinned_version_and_nothing_unpinned():
    locked = pins("graph.lock.txt") | pins("dev.lock.txt")
    installed = {norm(dist.metadata["Name"]): dist.version for dist in metadata.distributions()}
    assert {name: installed.get(name) for name in locked} == locked  # each library at the version pinned
    assert set(installed) - set(locked) <= {"klaxon", "pip"}  # and nothing else but Klaxon itself and pip

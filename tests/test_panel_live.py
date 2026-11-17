"""The panel's live view (week 7, item 2): a run is followed while it goes on, from its folder, and a waiting request
shows its command, its time left and a copy button. Checked against a made-up run that grows step by step: written
here, in a temporary folder, from a saved run's records. It is made up, and never saved as evidence."""

import html
import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from klaxon import fault_runs
from klaxon.agent import gate, loop
from klaxon.panel import runs, server

BASE = f"http://127.0.0.1:{server.PORT}"
SOURCE = "2026-11-15T222013Z_gated-run_money-unit"  # week 6: approved by Aravind on kind, verified, a postmortem
NAME = "2026-11-18T120000Z_gated-run_duplicate-delivery"  # the made-up run's folder


def _stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


class Growing:
    """A made-up run folder that grows the way a live run's does: the trace and recordings first, the request, the
    decision and the gate's trail, then run.json at the end, then the postmortem."""

    def __init__(self, tmp_path):
        self.source = runs.index()[SOURCE]
        self.root = tmp_path / "evidence"
        self.folder = self.root / "week7" / NAME
        self.folder.mkdir(parents=True)
        self.client = TestClient(server.create_app(root=self.root), base_url=BASE)

    def lines(self, name):
        return (self.source / name).read_text().splitlines()

    def write(self, name, lines):
        (self.folder / name).write_text("".join(f"{line}\n" for line in lines))

    def trace(self, steps: int, stop: bool = False):
        records = self.lines(loop.TRACE)
        kept = [records[0], *[r for r in records[1:] if json.loads(r)["kind"] == "step"][:steps]]
        self.write(loop.TRACE, kept + ([records[-1]] if stop else []))
        self.write(loop.RECORDING, self.lines(loop.RECORDING)[:1])
        self.write(loop.TOOL_RESULTS, self.lines(loop.TOOL_RESULTS)[:1])

    def request(self, expires_in: float):
        now = datetime.now(UTC)
        made = json.loads((self.source / gate.REQUEST).read_text())
        made |= {"created_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=expires_in)), "run": NAME}
        (self.folder / gate.REQUEST).write_text(json.dumps(made))
        self.write(gate.LOG, self.lines(gate.LOG)[:1])
        return made

    def decide(self):
        (self.folder / gate.DECISION).write_text((self.source / gate.DECISION).read_text())
        self.write(gate.LOG, self.lines(gate.LOG))

    def end(self, minutes_ago: float = 0):
        path = self.folder / fault_runs.RUN_FILE
        path.write_text((self.source / fault_runs.RUN_FILE).read_text())
        then = time.time() - minutes_ago * 60
        os.utime(path, (then, then))

    def page(self):
        answer = self.client.get(f"/run/{NAME}")
        assert answer.status_code == 200
        return html.unescape(answer.text)

    def view(self):
        return runs.run(self.folder, self.root)

    def path(self):
        return {p["name"]: (p["state"], p["note"]) for p in self.view()["phases"]}


@pytest.fixture
def run(tmp_path):
    return Growing(tmp_path)


def test_a_run_going_on_is_followed_step_by_step(run):
    run.trace(steps=0)
    assert run.view()["live"] and run.path()["Investigate"] == ("now", "step 0")
    page = run.page()
    assert 'data-live="true"' in page and "LIVE" in page and "No request yet." in page
    assert "A run going on now, on this Mac." in page
    assert "Not yet: the loop is still investigating." in page and "Going on now: 0 steps so far." in page
    first = run.client.get(f"/run/{NAME}/state").json()
    assert first == {"version": run.view()["version"], "live": True}
    run.trace(steps=2)
    second = run.client.get(f"/run/{NAME}/state").json()
    assert second["live"] and second["version"] != first["version"]
    assert run.path()["Investigate"] == ("now", "step 2")


def test_a_waiting_request_shows_its_command_time_left_and_a_copy_button(run):
    run.trace(steps=4, stop=True)
    made = run.request(expires_in=600)
    view = run.view()
    assert view["live"] and 590 < view["left"] <= 600
    assert run.path()["Diagnose"][0] == "done" and run.path()["Approve"] == ("now", "waiting for you")
    page = run.page()
    command = f"make approve RUN={view['typed']}"
    assert "Waiting for you." in page and f'data-copy="{command}"' in page and command in page
    assert f'data-expires="{made["expires_at"]}"' in page and "expires in 9:" in page
    assert "none yet" in page


def test_an_answered_request_shows_the_decision_and_no_copy_button(run):
    run.trace(steps=4, stop=True)
    run.request(expires_in=600)
    run.decide()
    assert run.path()["Approve"] == ("done", "approved") and run.path()["Act"] == ("done", "the action ran")
    assert run.path()["Verify"] == ("now", "health, then money")
    page = run.page()
    assert "data-copy=" not in page and "Waiting for you." not in page and "typed at a terminal" in page


def test_an_approved_request_is_followed_until_the_gate_says_how_it_ended(demo, tmp_path):
    # Found with make gate-demo on 2026-11-17: the page reloaded at the decision, then stopped before "done" came.
    client = TestClient(server.create_app(root=tmp_path / "evidence", also=[demo]), base_url=BASE)
    source = runs.index()[SOURCE]
    events = (source / gate.LOG).read_text().splitlines()
    (demo / gate.DECISION).write_text((source / gate.DECISION).read_text())
    (demo / gate.LOG).write_text("\n".join(events[:3]) + "\n")  # requested, approved, decision taken
    assert client.get(f"/run/{demo.name}/state").json()["live"] is True
    (demo / gate.LOG).write_text("\n".join(events) + "\n")  # running, then done
    assert client.get(f"/run/{demo.name}/state").json()["live"] is False
    page = html.unescape(client.get(f"/run/{demo.name}").text)
    assert "the action ran" in page


def test_an_expired_request_is_denied_and_waits_no_more(run):
    run.trace(steps=4, stop=True)
    run.request(expires_in=-5)
    view = run.view()
    assert view["left"] is None and run.path()["Approve"] == ("no", "expired: no answer")
    page = run.page()
    assert "data-copy=" not in page and "none: no answer in time means denied" in page


def test_the_end_of_a_run_then_its_postmortem_stop_the_following(run):
    run.trace(steps=4, stop=True)
    run.request(expires_in=600)
    run.decide()
    run.end()
    assert run.view()["live"]  # it acted, and its postmortem comes a little after the run
    assert run.path()["Verify"] == ("done", "passed")
    (run.folder / "postmortem.md").write_text("# A draft\n")
    assert not run.view()["live"] and 'data-live="false"' in run.page()
    assert run.client.get(f"/run/{NAME}/state").json()["live"] is False


def test_a_run_that_ended_long_ago_is_not_followed(run):
    run.trace(steps=4, stop=True)
    run.request(expires_in=-5)
    run.decide()
    run.end(minutes_ago=runs.LIVE_MINUTES + 1)
    assert not run.view()["live"]


def test_a_run_with_no_run_json_and_no_change_for_long_is_not_going(run):
    run.trace(steps=2)
    then = time.time() - (runs.LIVE_MINUTES + 1) * 60
    for path in run.folder.iterdir():
        os.utime(path, (then, then))
    assert not run.view()["live"] and not run.view()["going"]
    assert runs.NO_RUN_FILE in run.page()


def test_the_saved_runs_are_not_followed(run):
    view = runs.run(runs.index()[SOURCE])
    assert not view["live"] and not view["going"] and view["left"] is None


def test_the_state_answers_get_only_and_only_for_a_run(run):
    run.trace(steps=1)
    assert run.client.get("/run/no-such-run/state").status_code == 404
    assert run.client.post(f"/run/{NAME}/state").status_code == 405
    assert run.client.get(f"/run/{NAME}/state").headers["content-type"].startswith("application/json")


def test_the_live_script_only_reads_this_panel(run):
    answer = run.client.get("/static/live.js")
    assert answer.status_code == 200 and answer.headers["content-type"].startswith("text/javascript")
    script = answer.text
    assert "fetch(" in script and "data-state" in script and "navigator.clipboard.writeText" in script
    assert "http" not in script and "method" not in script and "innerHTML" not in script and "eval(" not in script


# make gate-demo's folder, named on the panel's command line (make panel ALSO=)


@pytest.fixture
def demo(tmp_path):
    folder = tmp_path / "klaxon-gate-demo-abc123"
    folder.mkdir()
    source = runs.index()[SOURCE]
    now = datetime.now(UTC)
    made = json.loads((source / gate.REQUEST).read_text())
    made |= {"created_at": _stamp(now), "expires_at": _stamp(now + timedelta(seconds=300))}
    (folder / gate.REQUEST).write_text(json.dumps(made))
    (folder / gate.LOG).write_text((source / gate.LOG).read_text().splitlines()[0] + "\n")
    return folder


def test_the_gate_demo_shows_as_a_simulation_with_its_waiting_request(demo, tmp_path):
    client = TestClient(server.create_app(root=tmp_path / "evidence", also=[demo]), base_url=BASE)
    listed = html.unescape(client.get("/runs").text)
    assert demo.name in listed and "a simulation: make gate-demo" in listed
    page = html.unescape(client.get(f"/run/{demo.name}").text)
    assert runs.SIMULATION in page and "Simulation" in page and "Waiting for you." in page
    assert f"make approve RUN={demo.resolve()}" in page
    assert "MoneyCheckFailed" in page  # the alert, from the request
    # A made-up diagnosis is never called a recorded Claude response, and there is nothing to replay.
    assert "Made up for the demo" in page and "Recorded Claude response" not in page
    assert "make replay-run" not in page and "nothing to replay" in page


def test_a_folder_outside_evidence_is_shown_only_when_named(demo, tmp_path):
    client = TestClient(server.create_app(root=tmp_path / "evidence"), base_url=BASE)
    assert client.get(f"/run/{demo.name}").status_code == 404
    assert client.get(f"/run/{demo.name}/state").status_code == 404


def test_also_comes_from_the_command_line(monkeypatch, demo):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(kw, app=app))
    assert server.main(["--also", str(demo)]) == 0
    assert TestClient(seen["app"], base_url=BASE).get(f"/run/{demo.name}").status_code == 200

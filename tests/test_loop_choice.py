"""Which loop runs a run (week 6, item 2), through the harness, offline: make investigate-fault and make eval take
LOOP=langgraph, every run's files and every suite's say which loop ran it, a loop that cannot run here is refused before
anything is touched, and the report never counts two loops in one row.

The stack, the traffic and the model's replies are made up (tests.test_fault_runs' World, scripted replies); the loops,
the harness and Klaxon's real MCP server code are real, in this process. No model is called and nothing is run.
"""

import json
import re

import pytest

from klaxon import evaluation, fault_runs
from klaxon.agent import graph, loop
from klaxon.agent.trace import read_trace
from tests.test_evaluation import patch_suite
from tests.test_fault_runs import DockerStack, World, patch_command

WEEK5 = evaluation.REPO / "evidence" / "week5"


# make investigate-fault LOOP=langgraph


@pytest.mark.parametrize("asked, ran", [([], loop.PLAIN), (["--loop", "langgraph"], loop.LANGGRAPH)])
def test_a_fault_run_runs_on_the_loop_asked_for_and_every_one_of_its_files_says_so(monkeypatch, tmp_path, asked, ran):
    given = patch_command(monkeypatch, World())
    assert fault_runs.main(["run", "provider-outage", "--no-actions", *asked, "--evidence-dir", str(tmp_path)]) == 0
    [folder] = tmp_path.iterdir()
    saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
    expected = loop.loop_header(ran)
    assert {key: saved.get(key) for key in ("loop_kind", "langgraph_version")} == {
        "loop_kind": ran,
        "langgraph_version": expected.get("langgraph_version"),
    }
    [(_, header, _)] = given  # the header of the run's model recording and tool results
    assert {key: header[key] for key in expected} == expected
    assert read_trace(folder / loop.TRACE)[0]["extra"]["loop"]["kind"] == ran  # and the loop itself says the same
    assert saved["loop"]["reason"] == "diagnosed" and ("--loop langgraph" in saved["rerun_command"]) == bool(asked)


def test_a_fault_run_on_a_loop_that_cannot_run_here_is_refused_before_anything_is_touched(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setattr(fault_runs, "preflight", lambda model: pytest.fail("pre-flight ran"))
    monkeypatch.setattr(fault_runs.faults, "LocalStack", lambda: pytest.fail("the stack was touched"))
    assert fault_runs.main(["run", "poison-message", "--loop", "langgraph", "--evidence-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
    err = capsys.readouterr().err
    assert err.startswith(
        "Not started, and nothing was touched: LangSmith tracing is switched on (LANGCHAIN_TRACING_V2)"
    )


# make eval LOOP=langgraph


def test_a_suite_on_the_langgraph_loop_says_so_in_its_folder_its_file_each_run_and_the_report(monkeypatch, tmp_path):
    world = DockerStack()
    patch_suite(monkeypatch, world)
    argv = ["run", "--cases", "duplicate-delivery,false-alarm", "--seeds", "7", "--loop", "langgraph"]
    assert evaluation.main([*argv, "--evidence-dir", str(tmp_path)]) == 0
    [suite_folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_suite_claude-opus-5-5_langgraph", suite_folder.name)
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert (suite["loop_kind"], suite["langgraph_version"], suite["evaluation_version"]) == ("langgraph", "1.2.18", 2)
    for run in suite["runs"]:
        folder = suite_folder / run["folder"]
        saved = json.loads((folder / fault_runs.RUN_FILE).read_text())
        assert (saved["loop_kind"], saved["langgraph_version"]) == ("langgraph", graph.VERSION)
        assert saved["rerun_command"].endswith("--loop langgraph")
        assert read_trace(folder / loop.TRACE)[0]["extra"]["loop"]["kind"] == loop.LANGGRAPH
    found = json.loads((suite_folder / evaluation.REPORT_JSON).read_text())
    assert [entry["loop_kind"] for entry in found["summary"]] == ["langgraph", "langgraph"]
    page = (suite_folder / evaluation.REPORT_MD).read_text()
    assert page.count("| evaluation, week 6, LangGraph loop |") == 4  # each case's row, and each run's


def test_a_suite_on_the_plain_loop_is_named_and_reported_as_before_and_records_its_loop(monkeypatch, tmp_path):
    world = DockerStack()
    patch_suite(monkeypatch, world)
    assert evaluation.main(["run", "--cases", "false-alarm", "--seeds", "7", "--evidence-dir", str(tmp_path)]) == 0
    [suite_folder] = tmp_path.iterdir()
    assert suite_folder.name.endswith("_suite_claude-opus-5-5")
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert suite["loop_kind"] == "plain" and "langgraph_version" not in suite
    [run] = suite["runs"]
    saved = json.loads((suite_folder / run["folder"] / fault_runs.RUN_FILE).read_text())
    assert saved["loop_kind"] == "plain" and "--loop" not in saved["rerun_command"]
    page = (suite_folder / evaluation.REPORT_MD).read_text()
    assert "LangGraph" not in page and page.count("| evaluation, week 6 |") == 2


def test_a_suite_on_a_loop_that_cannot_run_here_is_refused_before_anything_is_touched(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(evaluation.fault_runs, "preflight", lambda model: pytest.fail("pre-flight ran"))
    monkeypatch.setattr(evaluation, "fresh_stack", lambda: pytest.fail("the stack was touched"))
    assert evaluation.main(["run", "--loop", "langgraph", "--evidence-dir", str(tmp_path)]) == 2
    assert list(tmp_path.iterdir()) == []
    assert "Not started, and nothing was touched: LangSmith tracing is switched on" in capsys.readouterr().err


# The report: never two loops in one row


def copy_run(source, target, **changes):
    """A saved run's run.json and model recording, copied, with its run.json changed: made up from a real one."""
    target.mkdir(parents=True)
    saved = json.loads((source / fault_runs.RUN_FILE).read_text()) | changes
    (target / fault_runs.RUN_FILE).write_text(json.dumps(saved))
    (target / loop.RECORDING).write_bytes((source / loop.RECORDING).read_bytes())
    return target


def test_the_report_never_counts_two_loops_in_one_row_and_puts_the_plain_loops_first(tmp_path):
    source = sorted(WEEK5.glob("*_suite_claude-opus-5-5/*_false-alarm_seed7"))[0]
    copy_run(source, tmp_path / "a_false-alarm_seed7", loop_kind="langgraph", langgraph_version="1.2.18")
    copy_run(source, tmp_path / "b_false-alarm_seed7")
    found = evaluation.report([tmp_path])
    assert [row["loop_kind"] for row in found["runs"]] == ["langgraph", "plain"]  # in the folders' order
    entries = [(e["case"], e["mode"], e["loop_kind"], e["runs"]) for e in found["summary"]]
    assert entries == [("false-alarm", "evaluation", "plain", 1), ("false-alarm", "evaluation", "langgraph", 1)]
    page = evaluation.markdown(found)
    assert "| false-alarm | evaluation | 1 ran" in page and "| false-alarm | evaluation, LangGraph loop | 1 ran" in page


def test_runs_saved_before_runs_named_their_loop_count_as_the_plain_loops():
    found = evaluation.report([WEEK5])
    assert len(found["runs"]) == 48 and {row["loop_kind"] for row in found["runs"]} == {"plain"}
    assert "LangGraph" not in evaluation.markdown(found)


def test_a_mode_cell_names_the_loop_only_when_it_is_not_the_plain_one():
    assert evaluation.mode_cell({"mode": "evaluation, week 6", "loop_kind": "plain"}) == "evaluation, week 6"
    langgraph = {"mode": "evaluation, week 6", "loop_kind": "langgraph"}
    assert evaluation.mode_cell(langgraph) == "evaluation, week 6, LangGraph loop"
    assert evaluation.mode_cell({"mode": "gated, week 4", "loop_kind": "x|y"}) == "gated, week 4, x/y loop"

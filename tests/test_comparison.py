"""Week 8, item 3, offline: the comparison suite and its parts.

For each case and seed, the loops in turn (plain, LangGraph, multi-agent), each run on a fresh stack, so anything that
changes during a night, such as the seat's limits, touches each loop alike (Aravind's choice, 2026-11-20). A suite of
only what an earlier one left not run, in its order. Where the runs go. The model replies are scripted
(ScriptedModel); none came from a model, and no stack is used.
"""

import json
import os
import re
import subprocess

import pytest

from klaxon import evaluation, fault_runs
from klaxon.agent import loop
from klaxon.agent.model import ScriptedModel
from tests.agent.test_loop import Served, tool
from tests.agent.test_team import report, review, triage
from tests.test_evaluation import NOTHING_WRONG_REPLY, patch_suite
from tests.test_fault_runs import NAMES_ROLLBACK, DockerStack

DUPLICATE, FALSE = evaluation.CASES["duplicate-delivery"], evaluation.CASES["false-alarm"]
THREE = (loop.PLAIN, loop.LANGGRAPH, loop.MULTI)
# Each loop's made-up replies: the multi-agent loop's roles take replies of their own.
SINGLE = {
    "MoneyCheckFailed": [tool("recent_deploys"), NAMES_ROLLBACK],
    "ConsumerLagHigh": [tool("query_metrics", query="lag"), NOTHING_WRONG_REPLY],
}
TEAM = {
    "MoneyCheckFailed": [triage(), tool("recent_deploys"), report("c1"), NAMES_ROLLBACK, review()],
    "ConsumerLagHigh": [triage(), tool("query_metrics", query="lag"), report("c1"), NOTHING_WRONG_REPLY, review()],
}


def patch_loops(monkeypatch, world):
    """patch_suite, with each loop's own scripted replies. Returns what each run was given: its alert's name, the
    loop its header names, and the loop it ran on."""
    patch_suite(monkeypatch, world)
    given = []

    def investigate_live(
        alert, budgets, folder, header, model_id, binary, actions=(), tool_client=None, loop_kind=None
    ):
        given.append((alert.name, header["loop_kind"], loop_kind))
        model = ScriptedModel((TEAM if loop_kind == loop.MULTI else SINGLE)[alert.name])
        return loop.investigate(
            alert, model, Served().client, folder / loop.TRACE, budgets, actions=actions, loop_kind=loop_kind
        )

    monkeypatch.setattr(evaluation.loop, "investigate_live", investigate_live)
    return given


def outcome(folder, loop_reason="diagnosed"):
    return {"folder": folder, "stopped": f"the loop stopped: {loop_reason}", "loop": loop_reason, "interrupted": False}


# The plan


def test_for_each_case_and_seed_the_loops_take_turns_seed_by_seed():
    planned = evaluation.plan_loops([DUPLICATE, FALSE], [7, 8], THREE)
    assert [(case.name, seed, kind) for case, seed, kind in planned] == [
        (case, seed, kind) for seed in (7, 8) for case in ("duplicate-delivery", "false-alarm") for kind in THREE
    ]


def test_a_single_loops_plan_is_unchanged():
    assert evaluation.plan([DUPLICATE, FALSE], [7]) == [(DUPLICATE, 7), (FALSE, 7)]


# The suite


def test_a_comparison_runs_each_loop_in_turn_and_the_suite_and_each_run_say_which(monkeypatch, tmp_path):
    given = patch_loops(monkeypatch, DockerStack())
    argv = ["run", "--cases", "duplicate-delivery,false-alarm", "--seeds", "7", "--loops", "plain,langgraph,multi"]
    assert evaluation.main([*argv, "--evidence-dir", str(tmp_path)]) == 0
    [suite_folder] = tmp_path.iterdir()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d{6}Z_suite_claude-opus-5-5_plain-langgraph-multi", suite_folder.name)
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert suite["loop_kinds"] == list(THREE) and "loop_kind" not in suite and suite["langgraph_version"]
    assert "(evaluation, version 2)" in suite["label"]
    assert "the loops in turn (plain loop, LangGraph loop, multi-agent loop)" in suite["label"]
    expected = [(case, kind) for case in ("duplicate-delivery", "false-alarm") for kind in THREE]
    assert [(entry["case"], entry["loop"]) for entry in suite["planned"]] == expected
    assert [(run["case"], run["loop_kind"]) for run in suite["runs"]] == expected and suite["stopped"] is None
    assert [(header, ran_on) for _, header, ran_on in given] == [(kind, kind) for _, kind in expected]
    for run in suite["runs"]:
        assert run["folder"].endswith(f"_seed7_{run['loop_kind']}")
        saved = json.loads((suite_folder / run["folder"] / fault_runs.RUN_FILE).read_text())
        assert (saved["loop_kind"], saved["loop"]["reason"]) == (run["loop_kind"], "diagnosed")
        rerun = saved["rerun_command"]  # one run again, on its own loop
        assert ("--loop" not in rerun) if run["loop_kind"] == loop.PLAIN else f"--loop {run['loop_kind']}" in rerun
    rows = evaluation.report([suite_folder])["summary"]
    assert sorted((row["case"], row["loop_kind"], row["runs"]) for row in rows) == sorted((*e, 1) for e in expected)


def test_a_comparison_that_stops_lists_each_run_it_left_with_its_loop():
    planned = evaluation.plan_loops([DUPLICATE, FALSE], [7], THREE)
    done, asked = iter([outcome("a"), outcome("b", "model_error")]), []

    def run_one(case, seed, *on_loop):
        asked.append((case.name, seed, *on_loop))
        return next(done)

    suite = evaluation.run_suite(planned, {}, lambda value: None, lambda: None, run_one)
    assert asked == [("duplicate-delivery", 7, loop.PLAIN), ("duplicate-delivery", 7, loop.LANGGRAPH)]
    assert suite["stopped"] == "after b, because its loop stopped on model_error"
    assert suite["not_run"] == [
        {"case": "duplicate-delivery", "seed": 7, "loop": loop.MULTI},
        *({"case": "false-alarm", "seed": 7, "loop": kind} for kind in THREE),
    ]


def test_a_comparison_says_which_loops_run_the_stack_could_not_be_made_fresh_for():
    made = []

    def fresh():
        made.append("fresh")
        if len(made) == 2:
            raise RuntimeError("docker is gone")

    planned = evaluation.plan_loops([DUPLICATE], [7], THREE)
    suite = evaluation.run_suite(planned, {}, lambda value: None, fresh, lambda case, seed, *on: outcome("a"))
    assert suite["stopped"] == (
        "the stack could not be made fresh for duplicate-delivery, seed 7, the LangGraph loop: RuntimeError: docker is gone"
    )
    assert [entry["loop"] for entry in suite["not_run"]] == [loop.LANGGRAPH, loop.MULTI]


@pytest.mark.parametrize("given", ["plain,bogus", "plain,plain", "multi"])
def test_loops_names_two_loops_or_more_each_once_or_nothing_starts(monkeypatch, tmp_path, capsys, given):
    world = DockerStack()
    patch_loops(monkeypatch, world)
    assert evaluation.main(["run", "--loops", given, "--evidence-dir", str(tmp_path / "out")]) == 2
    said = capsys.readouterr().err
    assert said.startswith("Not started, and nothing was touched:")
    assert ("no such loop: bogus" if "bogus" in given else "two loops or more, each once") in said
    assert not (tmp_path / "out").exists() and "fresh stack" not in world.calls


def test_one_loop_and_several_are_never_asked_for_together(capsys):
    with pytest.raises(SystemExit) as refused:
        evaluation.main(["run", "--loop", "multi", "--loops", "plain,multi"])
    assert refused.value.code == 2 and "not allowed with argument" in capsys.readouterr().err


def test_a_suite_goes_to_week_8s_folder_by_default():
    assert evaluation.EVIDENCE == evaluation.REPO / "evidence" / "week8"


# A suite of what an earlier one left not run


def saved_suite(folder, not_run, **over):
    folder.mkdir(parents=True)
    suite = {"evaluation_version": evaluation.VERSION, "model": "claude-opus-5-5", "not_run": not_run} | over
    (folder / evaluation.SUITE_FILE).write_text(json.dumps(suite))
    return folder


def test_a_suite_of_what_another_left_runs_just_that_in_its_order(monkeypatch, tmp_path):
    patch_loops(monkeypatch, DockerStack())
    left = [
        {"case": "false-alarm", "seed": 7, "loop": "multi"},
        {"case": "duplicate-delivery", "seed": 8, "loop": "plain"},
    ]
    earlier = saved_suite(tmp_path / "earlier", left)
    assert evaluation.main(["run", "--not-run-of", str(earlier), "--evidence-dir", str(tmp_path / "out")]) == 0
    [suite_folder] = (tmp_path / "out").iterdir()
    assert suite_folder.name.endswith("_suite_claude-opus-5-5_multi-plain_rest")
    suite = json.loads((suite_folder / evaluation.SUITE_FILE).read_text())
    assert suite["planned"] == left and suite["rest_of"] == str(earlier) and suite["model"] == "claude-opus-5-5"
    ran = [(run["case"], run["loop_kind"]) for run in suite["runs"]]
    assert ran == [("false-alarm", "multi"), ("duplicate-delivery", "plain")]


def test_what_a_single_loops_suite_left_runs_on_its_loop(monkeypatch, tmp_path):
    given = patch_loops(monkeypatch, DockerStack())
    earlier = saved_suite(tmp_path / "earlier", [{"case": "false-alarm", "seed": 9}], loop_kind=loop.LANGGRAPH)
    assert evaluation.main(["run", "--not-run-of", str(earlier), "--evidence-dir", str(tmp_path / "out")]) == 0
    assert [ran_on for _, _, ran_on in given] == [loop.LANGGRAPH]


@pytest.mark.parametrize(
    "over, argv, said",
    [
        ({"evaluation_version": 1}, [], "ran version 1 of the evaluation, not version 2"),
        ({}, ["--cases", "false-alarm"], "name none of them with it"),
        ({}, ["--loop", "multi"], "name none of them with it"),
        ({}, ["--model", "claude-sonnet-5-5"], "ran claude-opus-5-5, not claude-sonnet-5-5"),
        ({"not_run": []}, [], "left nothing not run"),
        ({"not_run": [{"case": "false-alarm", "seed": 7, "loop": "bogus"}]}, [], "lists a run Klaxon cannot plan"),
        ({"not_run": [{"case": "false-alarm", "seed": "7"}]}, [], "lists a run Klaxon cannot plan"),
        ({"not_run": [{"case": "no-such-case", "seed": 7}]}, [], "no case 'no-such-case'"),
    ],
)
def test_a_suite_of_what_another_left_is_refused_before_anything_is_touched(
    monkeypatch, tmp_path, capsys, over, argv, said
):
    world = DockerStack()
    patch_loops(monkeypatch, world)
    fields = dict(over)
    earlier = saved_suite(tmp_path / "earlier", fields.pop("not_run", [{"case": "false-alarm", "seed": 7}]), **fields)
    out = tmp_path / "out"
    assert evaluation.main(["run", "--not-run-of", str(earlier), *argv, "--evidence-dir", str(out)]) == 2
    assert said in capsys.readouterr().err and not out.exists() and "fresh stack" not in world.calls


# make eval


def made(*more):
    env = {key: value for key, value in os.environ.items() if key not in ("LOOP", "LOOPS", "NOT_RUN_OF")}
    done = subprocess.run(
        ["make", "--no-print-directory", "-s", "eval", *more, "PY=printf '%s\\n'"],
        cwd=evaluation.REPO, capture_output=True, text=True, env=env, check=True, timeout=60,
    )  # fmt: skip
    return done.stdout.splitlines()


def test_make_eval_passes_loops_and_not_run_of_on_as_one_argument_each_and_only_when_given():
    plain = made()
    assert "--loops" not in plain and "--not-run-of" not in plain
    given = made("LOOPS=plain,langgraph,multi", "NOT_RUN_OF=evidence/week8/a suite")
    assert given[given.index("--loops") + 1] == "plain,langgraph,multi"
    assert given[given.index("--not-run-of") + 1] == "evidence/week8/a suite"
    odd = made("LOOPS=plain'; echo pwned")
    assert odd[odd.index("--loops") + 1] == "plain'; echo pwned" and "pwned" not in odd


# The loops side by side in the report: made-up rows, then the 72 runs as saved. Nothing is called.

WEEK8 = evaluation.REPO / "evidence" / "week8" / "2026-11-20T161208Z_suite_claude-opus-5-5_plain-langgraph-multi"


def row_of(loop_kind, case="duplicate-delivery", **over):
    """A made-up report row: diagnosed, the rule and the judge agreeing that it names the cause, not labeled."""
    made_up = {"model": "m", "mode": "evaluation", "loop_kind": loop_kind, "case": case, "not_run": None}
    made_up |= {"diagnosed": True, "verdict": "names the cause", "judge": "names the cause", "aravind": None}
    made_up |= {"diagnosis_seconds": 10.0, "calls": 2, "cost_usd": 0.5}
    return made_up | over


def test_a_runs_settled_verdict_is_his_label_else_the_rule_and_the_judge_as_one_else_none():
    assert evaluation.settled(row_of(loop.PLAIN)) == "names the cause"  # the rule and the judge agree
    assert evaluation.settled(row_of(loop.PLAIN, judge="partly")) is None  # they differ: unsettled
    assert evaluation.settled(row_of(loop.PLAIN, judge="partly", aravind="partly")) == "partly"  # his label settles it
    assert evaluation.settled(row_of(loop.PLAIN, aravind="misses the cause")) == "misses the cause"  # his, over both
    assert evaluation.settled(row_of(loop.PLAIN, judge=None)) is None  # held back, or not judged
    assert evaluation.settled(row_of(loop.PLAIN, judge="no verdict")) is None
    none = {"diagnosed": False, "verdict": None, "judge": None}
    assert evaluation.settled(row_of(loop.PLAIN, **none)) == evaluation.NO_DIAGNOSIS  # it names nothing
    assert evaluation.settled(row_of(loop.PLAIN, not_run="no fresh stack")) is None  # counted apart


def test_the_loops_go_side_by_side_by_settled_verdicts_only_where_a_mode_ran_on_more_than_one():
    rows = [
        row_of(loop.MULTI),  # names the cause
        row_of(loop.MULTI, case="false-alarm", judge="partly"),  # unsettled
        row_of(loop.PLAIN, case="false-alarm", aravind="partly"),  # his: partly
        row_of(loop.PLAIN, diagnosed=False, verdict=None, judge=None, calls=3),  # no diagnosis
        row_of(loop.PLAIN, not_run="no fresh stack", calls=0, cost_usd=0.0),  # counted apart
        row_of(loop.LANGGRAPH, mode="another mode"),  # a mode run on one loop only: left out
    ]
    (entry,) = evaluation.compare_loops(rows)
    assert (entry["model"], entry["mode"], entry["cases"]) == ("m", "evaluation", ["duplicate-delivery", "false-alarm"])
    assert [found["loop_kind"] for found in entry["loops"]] == [loop.PLAIN, loop.MULTI]  # the loops' own order
    plain, multi = entry["loops"]
    assert (plain["runs"], plain["ran"], plain["names_the_cause"], plain["partly"], plain["no_diagnosis"]) == (
        3,
        2,
        0,
        1,
        1,
    )
    assert (plain["unsettled"], plain["labeled"], plain["calls"]) == (0, 1, 5)
    assert (multi["ran"], multi["names_the_cause"], multi["unsettled"], multi["median_calls"]) == (2, 1, 1, 2)
    assert multi["by_case"] == {
        "duplicate-delivery": {"ran": 1, "names_the_cause": 1},
        "false-alarm": {"ran": 1, "names_the_cause": 0},
    }
    assert evaluation.compare_loops([row_of(loop.PLAIN), row_of(loop.PLAIN)]) == []  # one loop: nothing side by side


def test_the_page_shows_the_loops_side_by_side_and_an_unsettled_run_never_as_a_miss():
    found = {"loops": evaluation.compare_loops([row_of(loop.PLAIN), row_of(loop.MULTI, judge="partly")])}
    page = "\n".join(evaluation.loops_markdown(found))
    assert "## The loops side by side" in page and "| plain loop | 1 ran, 0 not run | 1 of 1 |" in page
    assert "| multi-agent loop | 1 ran, 0 not run | 0 of 1 | 0 | 0 | 0 | 1 |" in page
    assert "| duplicate-delivery | 1 of 1 | 0 of 1 |" in page
    assert (
        evaluation.loops_markdown({"loops": []}) == [] and evaluation.loops_markdown({}) == []
    )  # a report from before


def test_the_72_runs_side_by_side_by_his_labels_and_the_graders_as_saved():
    # The week 8 comparison (2026-11-20 UTC), the judge's verdicts (2026-11-20) and his 24 labels (2026-11-22).
    found = evaluation.report([WEEK8], show_judge=True)
    (entry,) = found["loops"]
    said = {
        e["loop_kind"]: (e["ran"], e["names_the_cause"], e["partly"], e["misses_the_cause"], e["no_diagnosis"])
        + (e["unsettled"], e["labeled"], e["calls"])
        for e in entry["loops"]
    }
    assert said == {
        loop.PLAIN: (24, 24, 0, 0, 0, 0, 9, 173),
        loop.LANGGRAPH: (24, 21, 1, 0, 2, 0, 5, 179),
        loop.MULTI: (24, 21, 3, 0, 0, 0, 10, 290),
    }
    cases = {name: [e["by_case"][name]["names_the_cause"] for e in entry["loops"]] for name in entry["cases"]}
    assert cases.pop("false-alarm") == [3, 2, 3] and cases.pop("missing-deploys") == [3, 1, 0]
    assert len(cases) == 6 and all(counts == [3, 3, 3] for counts in cases.values())  # the six faults
    assert "## The loops side by side" in evaluation.markdown(found)
    held = {e["loop_kind"]: e["unsettled"] for e in evaluation.report([WEEK8])["loops"][0]["loops"]}
    assert held == {loop.PLAIN: 15, loop.LANGGRAPH: 17, loop.MULTI: 14}  # the judge held back on runs not labeled

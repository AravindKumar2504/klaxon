"""The mutation runner's command line (scripts/mutation_check.py): its help, and a usage error for what it cannot run.

Week 4 found that --help stopped with an IndexError: the script read its first two arguments as folders."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mutation_check.py"
USAGE = (
    "usage: python scripts/mutation_check.py REPO WORKDIR [--with-stack | --stack-only] [--shard K/N] [--only FILE ...]"
)


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_what_the_runner_does_and_how_to_call_it(flag):
    done = run(flag)
    assert done.returncode == 0
    assert done.stdout.startswith("Break the code on purpose, one way at a time")
    assert USAGE.replace("usage", "Usage", 1) in done.stdout
    assert "--stack-only runs" in done.stdout


@pytest.mark.parametrize("args", [[], ["/a/repo"], ["--only", "Makefile"], ["/a/repo", "--with-stack"]])
def test_without_a_repo_and_a_work_folder_first_it_says_how_to_call_it(args):
    done = run(*args)
    assert done.returncode == 2
    assert done.stderr.strip() == USAGE
    assert "Traceback" not in done.stderr


def load_runner():
    spec = importlib.util.spec_from_file_location("mutation_check", SCRIPT)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    return runner


def test_the_stack_only_runs_leave_out_every_offline_mutant():
    runner = load_runner()
    stack = runner.select(True, True, [])
    assert stack and all(m[4] for m in stack) and len(stack) == sum(1 for m in runner.MUTANTS if m[4])
    assert runner.select(True, True, ["src/klaxon/evaluation.py"]) == []  # a file with offline mutants only
    assert runner.select(False, True, []) == stack  # --stack-only needs no --with-stack


def test_the_runner_picks_the_offline_mutants_or_all_of_them_and_only_the_files_named():
    runner = load_runner()
    offline = runner.select(False, False, [])
    assert offline and not any(m[4] for m in offline)
    assert runner.select(True, False, []) == runner.MUTANTS
    named = runner.select(False, False, ["Makefile"])
    assert named and {m[1] for m in named} == {"Makefile"}


def test_the_runners_copy_holds_what_its_tests_need():
    # The week 5 review's H1: this file's tests call the runner's own script, so the copy must hold scripts/.
    text = SCRIPT.read_text()
    assert '"scripts", "compose.yaml"' in text


def test_each_mutants_text_is_in_its_file_exactly_once():
    # A change that moves a line a mutant breaks would stop a full run partway, as week 5's overnight run stopped
    # (2026-11-09, at mutant 1,013 of 1,118): make test catches such a mutant at once instead.
    runner = load_runner()
    repo = SCRIPT.parents[1]
    stale = [name for name, file, old, _new, _stack in runner.MUTANTS if (repo / file).read_text().count(old) != 1]
    assert stale == []


def test_a_mutants_run_leaves_out_the_guard_on_the_list_itself(monkeypatch, tmp_path):
    # Week 6, item 5: the guard above reads the runner's copy, where a mutant is applied, so under nearly every mutant
    # it failed first, and pytest -x stopped before the test files that sort after it (item 4's 39 mutants of 137).
    runner = load_runner()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    for integration in (False, True):
        runner.run_tests(tmp_path, "python", integration)
    assert len(calls) == 2
    assert all(args[args.index("--deselect") + 1] == runner.LIST_GUARD for args in calls)
    file, name = runner.LIST_GUARD.split("::")
    assert f"\ndef {name}(" in (SCRIPT.parents[1] / file).read_text()  # the guard it names is still there


def test_the_runners_copy_holds_every_folder_kind_ups_image_tag_reads():
    # Week 6, item 3: the tag reads requirements/, which the copy once left out, so its baseline would have failed.
    import re

    from klaxon.patient import cluster

    listed = re.search(r"for item in \((.*?)\):", SCRIPT.read_text(), re.DOTALL).group(1)
    copied = set(re.findall(r'"([^"]+)"', listed))
    needed = {path.relative_to(cluster.REPO).parts[0] for path in cluster.build_inputs()}
    assert needed <= copied and "requirements" in copied


# Week 7, item 3: --shard K/N, so CI runs the full list as N parts side by side.


def test_shard_reads_part_k_of_n():
    runner = load_runner()
    assert runner.shard_of(["--with-stack"]) is None
    assert runner.shard_of(["--shard", "3/10"]) == (3, 10)
    assert runner.shard_of(["--stack-only", "--shard", "1/1", "--only", "Makefile"]) == (1, 1)


@pytest.mark.parametrize("value", ["0/10", "11/10", "3", "3/0", "-1/10", "a/b", "3/10/2", "03/10", " 3/10", ""])
def test_a_shard_that_is_not_k_of_n_is_refused(value):
    runner = load_runner()
    with pytest.raises(ValueError, match="--shard takes K/N"):
        runner.shard_of(["--shard", value] if value else ["--shard"])


@pytest.mark.parametrize("value", ["0/10", "11/10", "x"])
def test_the_command_line_refuses_a_bad_shard_before_it_copies_anything(value, tmp_path):
    work = tmp_path / "work"
    done = run(str(tmp_path / "repo"), str(work), "--shard", value)
    assert done.returncode == 2
    assert "--shard takes K/N" in done.stderr and USAGE in done.stderr
    assert "Traceback" not in done.stderr and not work.exists()


@pytest.mark.parametrize("parts", [1, 2, 3, 10, 2000])
def test_the_n_parts_together_are_every_mutant_picked_each_once(parts):
    runner = load_runner()
    picked = runner.select(False, False, [])
    shards = [runner.in_shard(picked, (part, parts)) for part in range(1, parts + 1)]
    names = [m[0] + m[1] + m[2] for shard in shards for m in shard]
    assert sorted(names) == sorted(m[0] + m[1] + m[2] for m in picked) and len(set(names)) == len(names)
    assert max(map(len, shards)) - min(map(len, shards)) <= 1  # even parts
    assert runner.in_shard(picked, None) == picked


def test_shards_split_what_only_and_stack_only_pick():
    runner = load_runner()
    for picked in (runner.select(False, False, ["src/klaxon/agent/claude_code.py"]), runner.select(True, True, [])):
        assert picked
        first, second = runner.in_shard(picked, (1, 2)), runner.in_shard(picked, (2, 2))
        assert first == picked[0::2] and second == picked[1::2]  # in turn, by place in what was picked


@pytest.mark.parametrize(
    "args", [["--shard", "1/2", "--only", "no/such.py"], ["--only", "no/such.py", "--shard", "1/2"]]
)
def test_the_shard_is_not_read_as_a_file_for_only(args, tmp_path):
    done = run(str(tmp_path / "repo"), str(tmp_path / "work"), *args)
    assert done.returncode == 2
    assert done.stdout.strip() == "no mutants for: no/such.py"  # not "--shard, 1/2, no/such.py"

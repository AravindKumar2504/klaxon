"""The mutation runner's command line (scripts/mutation_check.py): its help, and a usage error for what it cannot run.

Week 4 found that --help stopped with an IndexError: the script read its first two arguments as folders."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mutation_check.py"
USAGE = "usage: python scripts/mutation_check.py REPO WORKDIR [--with-stack | --stack-only] [--only FILE ...]"


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

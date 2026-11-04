"""The mutation runner's command line (scripts/mutation_check.py): its help, and a usage error for what it cannot run.

Week 4 found that --help stopped with an IndexError: the script read its first two arguments as folders."""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mutation_check.py"
USAGE = "usage: python scripts/mutation_check.py REPO WORKDIR [--with-stack] [--only FILE ...]"


def run(*args):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_prints_what_the_runner_does_and_how_to_call_it(flag):
    done = run(flag)
    assert done.returncode == 0
    assert done.stdout.startswith("Break the code on purpose, one way at a time")
    assert "Usage: python scripts/mutation_check.py REPO WORKDIR [--with-stack] [--only FILE ...]" in done.stdout


@pytest.mark.parametrize("args", [[], ["/a/repo"], ["--only", "Makefile"], ["/a/repo", "--with-stack"]])
def test_without_a_repo_and_a_work_folder_first_it_says_how_to_call_it(args):
    done = run(*args)
    assert done.returncode == 2
    assert done.stderr.strip() == USAGE
    assert "Traceback" not in done.stderr

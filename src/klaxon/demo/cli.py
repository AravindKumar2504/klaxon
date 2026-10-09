"""python -m klaxon.demo duplicate-webhook --handler naive|idempotent

Exit status: 0 when the money check passes, 1 when it fails, 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path

from klaxon.demo import duplicate_webhook
from klaxon.patient.ledger import MODES


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="python -m klaxon.demo",
        description="Synthetic, seeded demos of the checks Klaxon will use. Not the agent.",
    )
    scenarios = parser.add_subparsers(dest="scenario", required=True)
    dup = scenarios.add_parser("duplicate-webhook", help="the provider delivers one webhook twice")
    dup.add_argument("--handler", choices=MODES, required=True)
    dup.add_argument("--seed", type=int, default=7)
    dup.add_argument("--payments", type=_positive_int, default=5)
    dup.add_argument("--evidence-dir", type=Path, default=Path("evidence/first-piece"))
    dup.add_argument("--no-evidence", action="store_true", help="print only; write no evidence file")
    args = parser.parse_args(argv)

    result = duplicate_webhook.run(args.handler, seed=args.seed, payments=args.payments)
    print(duplicate_webhook.render(result))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = duplicate_webhook.write_evidence(result, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if result.report.passed else 1

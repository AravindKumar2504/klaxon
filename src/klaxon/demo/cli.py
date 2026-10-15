"""python -m klaxon.demo duplicate-webhook --handler naive|idempotent
python -m klaxon.demo live-duplicate-webhook --handler naive|idempotent
python -m klaxon.demo live-lost-answer --confirmer by_our_id|by_provider_id
python -m klaxon.demo live-check-under-traffic --checks 20 --rate 5
python -m klaxon.demo live-fault --fault duplicate-delivery|poison-message|provider-outage

Exit status: 0 when the money check passes, 1 when it fails, 2 for bad arguments
(or, for the live demos, when they could not run as designed). For live-fault: 0 when
the money check said what the fault expects in every phase, 1 when it did not.
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
    live = scenarios.add_parser(
        "live-duplicate-webhook", help="the same scenario through the live Docker Compose stack (needs a fresh stack)"
    )
    live.add_argument("--handler", choices=MODES, required=True, help="must match the running ledger consumer")
    live.add_argument("--seed", type=int, default=7)
    live.add_argument("--payments", type=_positive_int, default=5)
    live.add_argument("--evidence-dir", type=Path, default=Path("evidence/week1"))
    live.add_argument("--no-evidence", action="store_true", help="print only; write no evidence file")
    lost = scenarios.add_parser(
        "live-lost-answer",
        help="the provider charges a payment but its answer is lost: is it confirmed? (needs a clean stack)",
    )
    lost.add_argument(
        "--confirmer", choices=("by_our_id", "by_provider_id"), required=True, help="must match the running confirmer"
    )
    lost.add_argument("--seed", type=int, default=7)
    lost.add_argument("--evidence-dir", type=Path, default=Path("evidence/week2"))
    lost.add_argument("--no-evidence", action="store_true", help="print only; write no evidence file")
    under = scenarios.add_parser(
        "live-check-under-traffic", help="run the money check again and again while synthetic traffic flows"
    )
    under.add_argument("--checks", type=_positive_int, default=20, help="checks while the traffic flows")
    under.add_argument("--rate", type=float, default=5.0, help="new payments per second, on average")
    under.add_argument("--seed", type=int, default=7)
    under.add_argument("--evidence-dir", type=Path, default=Path("evidence/week2"))
    under.add_argument("--no-evidence", action="store_true", help="print only; write no evidence file")
    fault = scenarios.add_parser(
        "live-fault", help="inject one fault while traffic flows, and record what it looks like"
    )
    fault.add_argument("--fault", required=True, choices=("duplicate-delivery", "poison-message", "provider-outage"))
    fault.add_argument("--rate", type=float, default=3.0, help="new payments per second, on average")
    fault.add_argument("--seed", type=int, default=7)
    fault.add_argument("--during", type=float, default=60.0, help="seconds to watch with the fault in place")
    fault.add_argument("--after", type=float, default=60.0, help="seconds to watch after clearing it")
    fault.add_argument("--evidence-dir", type=Path, default=Path("evidence/week2"))
    fault.add_argument("--no-evidence", action="store_true", help="print only; write no evidence file")
    args = parser.parse_args(argv)
    if args.scenario == "live-fault":
        return _fault(args, argv)
    if args.scenario == "live-check-under-traffic":
        return _under_traffic(args, argv)
    if args.scenario == "live-duplicate-webhook":
        return _live(args, argv)
    if args.scenario == "live-lost-answer":
        return _lost_answer(args, argv)

    result = duplicate_webhook.run(args.handler, seed=args.seed, payments=args.payments)
    print(duplicate_webhook.render(result))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = duplicate_webhook.write_evidence(result, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if result.report.passed else 1


def _live(args: argparse.Namespace, argv: list[str]) -> int:
    # Imported here, so the first piece's demo keeps working without the service libraries installed.
    from klaxon.demo import live

    try:
        run = live.run_live(args.handler, seed=args.seed, payments=args.payments)
    except Exception as exc:  # anything before a result exists means "could not run" (2), never "failed" (1)
        print(f"The live demo could not run as designed, so there is no result: {exc!r}", file=sys.stderr)
        return 2
    print(live.render(run))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = live.write_evidence(run, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if run.report.passed else 1


def _lost_answer(args: argparse.Namespace, argv: list[str]) -> int:
    from klaxon.demo import lost_answer

    try:
        run = lost_answer.run_live(args.confirmer, seed=args.seed)
    except Exception as exc:  # anything before a result exists means "could not run" (2), never "failed" (1)
        print(f"The live demo could not run as designed, so there is no result: {exc!r}", file=sys.stderr)
        return 2
    print(lost_answer.render(run))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = lost_answer.write_evidence(run, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if run.report.passed else 1


def _under_traffic(args: argparse.Namespace, argv: list[str]) -> int:
    from klaxon.demo import under_traffic

    try:
        run = under_traffic.run_live(rate=args.rate, checks=args.checks, seed=args.seed)
    except Exception as exc:  # anything before a result exists means "could not run" (2), never "failed" (1)
        print(f"The live demo could not run as designed, so there is no result: {exc!r}", file=sys.stderr)
        return 2
    print(under_traffic.render(run))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = under_traffic.write_evidence(run, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if run.passed else 1


def _fault(args: argparse.Namespace, argv: list[str]) -> int:
    from klaxon.demo import fault_scenario

    try:
        run = fault_scenario.run_live(
            args.fault, rate=args.rate, seed=args.seed, during_seconds=args.during, after_seconds=args.after
        )
    except Exception as exc:  # anything before a result exists means "could not run" (2), never "failed" (1)
        print(f"The live demo could not run as designed, so there is no result: {exc!r}", file=sys.stderr)
        return 2
    print(fault_scenario.render(run))
    if not args.no_evidence:
        command = shlex.join(["python", "-m", "klaxon.demo", *argv])
        path = fault_scenario.write_evidence(run, args.evidence_dir, command)
        print(f"\nEvidence written: {path}")
    return 0 if run.as_expected else 1  # did the money check say what the fault expects, in every phase?

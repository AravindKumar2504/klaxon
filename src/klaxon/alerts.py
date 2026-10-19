"""Klaxon's alerting: rules on symptoms only, never causes. An alert is what gives the loop its goal.

A symptom is what the people who pay, or the business, can see: payments that are slow, payments that fail, books
that fall behind, money that does not add up. A cause is why: a deploy, a setting, a dependency. These rules page on
symptoms only, as Google's SRE book advises: a rule written for a cause pages only for the causes someone thought of
in advance. And here an alert that named its cause would hand Klaxon the answer it is graded on (klaxon.faults
records each fault's cause; klaxon.grading compares the diagnosis with it).

    MoneyCheckFailed     the money check failed: our books and the provider's records disagree
    PaymentsErrorsHigh   over 20% of the answers to POST /payments were 5xx in the last minute
    PaymentsLatencyHigh  the p95 latency of POST /payments was over 2 s in the last minute
    ConsumerLagHigh      a consumer group is more than 50 messages behind on the topic provider-events

The metric rules read Prometheus on this Mac (GET only, no redirects followed, no proxies) and fire once their
condition has held at 2 evaluations in a row, so that one blip does not page (Prometheus calls this `for`). The
harness waits 15 s between evaluations, and an evaluation takes as long as its money check: up to 15 s more when the
books cannot settle. The error rule needs at least 0.2 requests per second: below that, one 503 would be a share of
100%. The money rule runs the money check through its read-only role, waiting up to 15 s for it to settle, and fires
the first time it fails. A check that cannot settle does not fire it: a stalled consumer shows as lag.

The thresholds come from week 2's measurements at 3 payments per second (evidence/week2/*_live-fault_*). On a healthy
stack, under the traffic's noise, the p95 of POST /payments was 0.06 to 0.09 s in three runs and 0.50 s in the
fourth; the consumers' lag stayed at 0 or 1. Under the faults, the p95 reached 9.4 s 20 s into the outage, and
the ledger's lag 50 messages 22 s into the poison. (Week 2 sampled 5xx answers over every route, not this rule's
share; week 3's first run with no fault measured the rules' own queries: evidence/week3/*_alerts-under-traffic_*.)
A value at a threshold does not breach it, and alert text writes a number with enough digits to show it is above.

Alert text is built from numbers and from names this module knows, never from text a source sends: a label's value
is only looked up among the names below (a group it does not know is left out), an invariant's name likewise, and a
number is used only once it is finite. Each alert is a klaxon.agent.loop.Alert: plain text, one line per field.
These rules know nothing of the faults: they read only what the stack shows.

Run: python -m klaxon.alerts               evaluate the rules once against the running stack, and print what they read
     python -m klaxon.alerts --watch 120   again and again for 120 s, 15 s apart
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

import httpx

from klaxon.agent.loop import MAX_ALERT, Alert
from klaxon.agent.tools import PROMETHEUS_URL
from klaxon.checks import live
from klaxon.checks.money import format_cents
from klaxon.traffic import require_local

EVERY_SECONDS = 15.0  # from the end of one evaluation to the start of the next
IN_A_ROW = 2  # evaluations a metric rule's condition must hold before it fires: one blip does not page
LATENCY_SECONDS = 2.0
ERROR_SHARE = 0.20
MIN_REQUESTS_PER_SECOND = 0.2  # below this, a share of errors says little
LAG_MESSAGES = 50
MONEY_SETTLE_SECONDS = 15.0  # how long the money check may wait to settle each time

_API = 'job="klaxon-patient/payments-api", http_route="/payments"'  # the route takes only POST
QUERIES = {
    "p95_seconds": (
        f"histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket{{{_API}}}[1m])))"
    ),
    "requests_per_second": f"sum(rate(http_server_request_duration_seconds_count{{{_API}}}[1m]))",
    "errors_per_second": (
        f'sum(rate(http_server_request_duration_seconds_count{{{_API}, http_response_status_code=~"5.."}}[1m]))'
    ),
    "lag_by_group": "sum by (consumer) (klaxon_kafka_consumer_lag)",
}
# The names alert text may use for what Prometheus labels: a label's value is looked up here, never repeated.
CONSUMER_GROUPS = {"ledger": "ledger", "payment-confirmer": "payment-confirmer"}
INVARIANTS = frozenset(
    {"posted_exactly_once", "ledger_balanced", "matches_provider", "payments_match_provider", "refunds_match_provider"}
)
ORDER = ("MoneyCheckFailed", "PaymentsErrorsHigh", "PaymentsLatencyHigh", "ConsumerLagHigh")  # how an alert lists them

Series = list[tuple[dict, float | None]]  # an instant query's answer: each series' labels and its value


@dataclass(frozen=True)
class Check:
    """One rule's reading at one evaluation, for one subject (a consumer group, say): the numbers it read, and
    whether its condition held. sentence: what the alert says if the rule fires, made from those numbers only."""

    rule: str
    subject: str
    numbers: dict
    breached: bool
    needed: int  # evaluations in a row the condition must hold before the rule fires
    sentence: str = ""
    in_a_row: int = 0  # evaluations in a row the condition has held, this one included

    @property
    def firing(self) -> bool:
        return self.breached and self.in_a_row >= self.needed


@dataclass(frozen=True)
class Evaluation:
    at_utc: str
    checks: tuple[Check, ...]
    problems: tuple[str, ...]  # what could not be read, in this module's own words
    alert: Alert | None  # every rule that fires now, as one alert

    def to_dict(self) -> dict:
        return {
            "at_utc": self.at_utc,
            "checks": [asdict(check) | {"firing": check.firing} for check in self.checks],
            "problems": list(self.problems),
            "alert": None if self.alert is None else asdict(self.alert),
        }


def finite(text: object) -> float | None:
    """A value as Prometheus writes it (a string), or None: not a number, NaN, or infinite."""
    if not isinstance(text, str):
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def above(value: float, threshold: float, places: int) -> str:
    """value, written with enough decimal places to show it is above threshold: never "2.0 s, above 2 s"."""
    for more in range(places, 7):
        text = f"{value:,.{more}f}"
        if float(text.replace(",", "")) > threshold:
            return text
    return repr(value)  # the shortest text that reads back as exactly this number


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}" if n == 1 else f"{n} {thing}s"


# The rules: each turns numbers into a Check.


def latency(p95: float | None) -> Check:
    breached = p95 is not None and p95 > LATENCY_SECONDS
    sentence = (
        f"Payments are slow: over the last minute, the p95 latency of POST /payments on payments-api was"
        f" {above(p95, LATENCY_SECONDS, 1)} s, above the threshold of {LATENCY_SECONDS:g} s."
        if breached
        else ""
    )
    return Check("PaymentsLatencyHigh", "payments-api", {"p95_seconds": _round(p95)}, breached, IN_A_ROW, sentence)


def errors(per_second: float | None, requests: float | None) -> Check:
    share = None
    if per_second is not None and requests is not None and requests >= MIN_REQUESTS_PER_SECOND:
        share = min(per_second / requests, 1.0)
    breached = share is not None and share > ERROR_SHARE
    sentence = (
        f"Payments are failing: over the last minute, {above(share * 100, ERROR_SHARE * 100, 0)}% of the answers to"
        f" POST /payments on payments-api were 5xx ({per_second:.2f} per second), above the threshold of {ERROR_SHARE:.0%}."
        if breached
        else ""
    )
    numbers = {"errors_per_second": _round(per_second), "requests_per_second": _round(requests), "share": _round(share)}
    return Check("PaymentsErrorsHigh", "payments-api", numbers, breached, IN_A_ROW, sentence)


def lags(series: Series) -> list[Check]:
    """One check per consumer group this module knows; a group it does not know is left out, its label unsaid."""
    out: dict[str, Check] = {}
    for labels, value in series:
        group = labels.get("consumer")
        name = CONSUMER_GROUPS.get(group) if isinstance(group, str) else None
        if name is None or name in out:
            continue
        breached = value is not None and value > LAG_MESSAGES
        sentence = (
            f"The {name} consumer group is {above(value, LAG_MESSAGES, 0)} messages behind on the topic provider-events, all partitions"
            f" together; the threshold is {LAG_MESSAGES}."
            if breached
            else ""
        )
        out[name] = Check("ConsumerLagHigh", name, {"messages_behind": _round(value)}, breached, IN_A_ROW, sentence)
    return [out[name] for name in sorted(out)]


def money(reading: Mapping) -> Check:
    breached = reading.get("outcome") == "failed"
    return Check(
        "MoneyCheckFailed", "money check", dict(reading), breached, 1, _money_sentence(reading) if breached else ""
    )


def _money_sentence(reading: Mapping) -> str:
    failed = reading.get("failed") or {}
    named = [f"{name} ({_count(n, 'finding')})" for name, n in failed.items() if name in INVARIANTS]
    others = sum(1 for name in failed if name not in INVARIANTS)
    if others:
        named.append(_count(others, "other invariant"))
    sentence = (
        f"The money check failed {len(failed)} of its {reading.get('invariants')} invariants: {', '.join(named)}."
    )
    off = reading.get("money_off_cents")
    if isinstance(off, int) and off:
        side = "ahead of" if off > 0 else "behind"
        sentence += f" By its count, our ledger is {format_cents(abs(off))} {side} the provider's balance."
    return sentence


def raise_alert(firing: Sequence[Check], at_utc: str) -> Alert | None:
    """One alert for every rule that fires at one evaluation, as an alerting system groups them: named after the
    first in ORDER, and saying what each one saw."""
    if not firing:
        return None
    ordered = sorted(firing, key=lambda check: (ORDER.index(check.rule), check.subject))
    sentences = [check.sentence for check in ordered]
    summary = " ".join(sentences)
    while len(summary) > MAX_ALERT["summary"]:  # never cut a sentence: leave the last ones out, and say so
        sentences.pop()
        left_out = len(ordered) - len(sentences)
        summary = " ".join([*sentences, f"({_count(left_out, 'more alert')} firing.)"])
    return Alert(ordered[0].rule, summary, at_utc)


# Reading the stack.


class Prometheus:
    """Instant queries on this Mac's Prometheus: GET only, no redirects followed, no proxies."""

    def __init__(self, url: str = PROMETHEUS_URL, http: httpx.Client | None = None) -> None:
        require_local(url)
        self.url = url.rstrip("/")
        self.http = http or httpx.Client(timeout=10.0, trust_env=False, follow_redirects=False)

    def __call__(self, query: str) -> Series:
        response = self.http.get(f"{self.url}/api/v1/query", params={"query": query})
        response.raise_for_status()  # any answer but 2xx, a redirect included
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "success":
            raise ValueError("Prometheus did not answer success")
        data = payload.get("data")
        if not isinstance(data, dict) or data.get("resultType") != "vector":
            raise ValueError("Prometheus did not answer with a vector")
        out: Series = []
        for item in data.get("result") or []:
            labels, value = item.get("metric"), item.get("value")
            point = value[1] if isinstance(value, list) and len(value) == 2 else None
            out.append((labels if isinstance(labels, dict) else {}, finite(point)))
        return out


def one(series: Series, empty: float | None = None) -> float | None:
    """The value of a query that gives at most one series; empty when it gives none."""
    if not series:
        return empty
    if len(series) > 1:
        raise ValueError("a query that gives one series gave several")
    return series[0][1]


def read_money(check: Callable[..., live.LiveCheck] = live.check_live) -> dict:
    """The money check's verdict in numbers and known names: passed, failed, could not settle, or could not run."""
    try:
        result = check(wait=True, timeout_seconds=MONEY_SETTLE_SECONDS)
    except live.NotSettled:
        return {"outcome": "could not settle"}
    except Exception as exc:  # out of reach, or an answer it refuses to read: no verdict either way
        return {"outcome": "could not run", "error": type(exc).__name__}
    report = result.report
    return {
        "outcome": "passed" if report.passed else "failed",
        "invariants": len(report.invariants),
        "failed": {inv.name: len(inv.evidence) for inv in report.invariants if not inv.passed},
        "money_off_cents": int(report.money_off_cents),
    }


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Alerting:
    """The rules, evaluated again and again: it remembers how many evaluations in a row each condition has held."""

    def __init__(
        self,
        prometheus: Callable[[str], Series] | None = None,
        money_check: Callable[[], dict] = read_money,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.prometheus = prometheus or Prometheus()
        self.money_check = money_check
        self.clock = clock
        self._held: dict[tuple[str, str], int] = {}

    def reset(self) -> None:
        """Forget how long each condition has held: at the next evaluation, every count starts from 0."""
        self._held = {}

    def evaluate(self) -> Evaluation:
        at_utc = utc(self.clock())
        checks, problems = self._read()
        counted = [
            replace(check, in_a_row=self._held.get((check.rule, check.subject), 0) + 1 if check.breached else 0)
            for check in checks
        ]
        # A subject not read this time (Prometheus out of reach, say) starts again from 0.
        self._held = {(check.rule, check.subject): check.in_a_row for check in counted}
        alert = raise_alert([check for check in counted if check.firing], at_utc)
        return Evaluation(at_utc, tuple(counted), tuple(problems), alert)

    def _read(self) -> tuple[list[Check], list[str]]:
        checks: list[Check] = []
        problems: list[str] = []
        try:
            p95 = one(self.prometheus(QUERIES["p95_seconds"]))
            requests = one(self.prometheus(QUERIES["requests_per_second"]))
            per_second = one(self.prometheus(QUERIES["errors_per_second"]), empty=0.0)  # no 5xx series: none
            lag = self.prometheus(QUERIES["lag_by_group"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"Prometheus could not be read ({type(exc).__name__}): the metric rules were not evaluated")
        else:
            checks += [latency(p95), errors(per_second, requests), *lags(lag)]
        reading = self.money_check()
        checks.append(money(reading))
        if reading.get("outcome") == "could not run":
            problems.append(f"the money check could not run ({reading.get('error')})")
        return checks, problems


def render(evaluation: Evaluation) -> str:
    """What one evaluation read, a line per check, and the alert if one fires."""
    lines = [f"Evaluation at {evaluation.at_utc}"]
    for check in evaluation.checks:
        numbers = ", ".join(f"{key} {value}" for key, value in check.numbers.items())
        state = "FIRING" if check.firing else f"held {check.in_a_row} of {check.needed}"
        lines.append(f"  {check.rule:<20} {check.subject:<18} {state:<12} {numbers}")
    lines += [f"  problem: {problem}" for problem in evaluation.problems]
    if evaluation.alert is not None:
        lines.append(f"  ALERT {evaluation.alert.name}: {evaluation.alert.summary}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m klaxon.alerts", description=__doc__.splitlines()[0])
    parser.add_argument("--watch", type=float, default=0.0, help="evaluate every 15 s for this many seconds")
    args = parser.parse_args(argv)
    alerting = Alerting()
    deadline = time.monotonic() + args.watch
    fired = False
    while True:
        evaluation = alerting.evaluate()
        fired = fired or evaluation.alert is not None
        print(render(evaluation), flush=True)
        if time.monotonic() + EVERY_SECONDS > deadline:
            return 1 if fired else 0
        time.sleep(EVERY_SECONDS)


if __name__ == "__main__":
    sys.exit(main())

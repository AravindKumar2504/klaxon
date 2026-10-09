"""What HTTP monitoring sees: request counts and status codes. Nothing about money.

The Grafana dashboard planned for week 2 will show the same kind of numbers.
This is the point of the first piece: every number here can be perfect while
the ledger holds a payment twice.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from klaxon.patient.ledger import RequestRecord

# Red when more than 1% of webhook requests fail. A typical alert threshold.
ERROR_RATE_THRESHOLD = 0.01


@dataclass(frozen=True)
class Dashboard:
    requests: int
    status_counts: dict[int, int]
    ok: int  # 2xx
    client_errors: int  # 4xx
    server_errors: int  # 5xx
    error_rate: float
    status: str  # "GREEN" or "RED"


def summarize(records: Iterable[RequestRecord]) -> Dashboard:
    statuses = Counter(record.status for record in records)
    total = sum(statuses.values())
    ok = sum(n for code, n in statuses.items() if 200 <= code < 300)
    client = sum(n for code, n in statuses.items() if 400 <= code < 500)
    server = sum(n for code, n in statuses.items() if code >= 500)
    error_rate = (total - ok) / total if total else 0.0
    return Dashboard(
        requests=total,
        status_counts=dict(sorted(statuses.items())),
        ok=ok,
        client_errors=client,
        server_errors=server,
        error_rate=error_rate,
        status="GREEN" if error_rate <= ERROR_RATE_THRESHOLD else "RED",
    )

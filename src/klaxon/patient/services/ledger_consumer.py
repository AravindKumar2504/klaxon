"""The ledger consumer: reads provider events from Kafka and books them in PostgreSQL.

Its consumer group is "ledger". The loop it runs is shared with the payment
confirmer (`klaxon.patient.services.consumer`): book the event in one
database transaction, then commit the Kafka offset; dead-letter only what can
never be booked; retry everything else without committing. Read twice +
idempotent booking = booked once (see `pg_ledger`).

Run: python -m klaxon.patient.services.ledger_consumer
"""

from __future__ import annotations

import logging

from klaxon.patient.services.common import configure_logging, env_str, log
from klaxon.patient.services.consumer import open_pool, serve
from klaxon.patient.services.kafka_io import LEDGER_GROUP
from klaxon.patient.services.pg_ledger import PgLedger

logger = logging.getLogger("klaxon.ledger_consumer")


def main() -> None:
    configure_logging("ledger-consumer")
    mode = env_str("LEDGER_HANDLER", "idempotent")
    pool = open_pool("ledger")
    log(logger, logging.INFO, "ledger consumer starting", mode=mode)
    try:
        serve(
            PgLedger(pool, mode).book, group=LEDGER_GROUP, verb="book", heartbeat_path="/tmp/ledger-consumer.heartbeat"
        )
    finally:
        pool.close()
        log(logger, logging.INFO, "ledger consumer stopped")


if __name__ == "__main__":
    main()

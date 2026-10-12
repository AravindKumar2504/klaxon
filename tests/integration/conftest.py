"""Integration tests run against the Compose stack's PostgreSQL and Kafka (make up, then make test-integration).

Each PostgreSQL test gets a scratch database built from docker/postgres/initdb/20-schema.sql and dropped
afterwards, so the stack's own data is never touched. The passwords are the local development ones in compose.yaml.
"""

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from klaxon.patient.stack import REPO

PASSWORDS = {
    "postgres": "postgres-dev-only",
    "ledger_writer": "ledger-writer-dev-only",
    "payments_api": "payments-api-dev-only",
    "payment_confirmer": "payment-confirmer-dev-only",
    "klaxon_readonly": "klaxon-readonly-dev-only",
}
SCHEMA_SQL = (REPO / "docker" / "postgres" / "initdb" / "20-schema.sql").read_text()
KAFKA = "127.0.0.1:9094"


def dsn(role: str, database: str) -> str:
    return f"postgresql://{role}:{PASSWORDS[role]}@127.0.0.1:5432/{database}"


@pytest.fixture
def database() -> Iterator[str]:
    name = f"klaxon_test_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(dsn("postgres", "postgres"), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.fail(f"the stack's PostgreSQL is not reachable (make up): {exc}")
    with admin:
        missing = [
            r for r in PASSWORDS if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (r,)).fetchone()
        ]
        if missing:
            # The roles are made once, when the stack's database volume is first created (10-roles.sh).
            pytest.fail(f"the stack's database has no role {missing}: it predates them. Run make reset, then make up")
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        with psycopg.connect(dsn("postgres", name), autocommit=True) as conn:
            conn.execute(SCHEMA_SQL)
        yield name
    finally:
        with psycopg.connect(dsn("postgres", "postgres"), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


@pytest.fixture
def connect(database):
    """connect("ledger_writer") -> an autocommit connection as that role, closed after the test."""
    opened = []

    def open_as(role: str, **kwargs) -> psycopg.Connection:
        conn = psycopg.connect(dsn(role, database), autocommit=True, **kwargs)
        opened.append(conn)
        return conn

    yield open_as
    for conn in opened:
        conn.close()


@pytest.fixture
def pool_as(database):
    """pool_as("ledger_writer") -> a small pool like the services use (autocommit, transaction() blocks)."""
    pools = []

    def make(role: str, dict_rows: bool = False) -> ConnectionPool:
        kwargs = {"autocommit": True} | ({"row_factory": dict_row} if dict_rows else {})
        pool = ConnectionPool(dsn(role, database), min_size=1, max_size=4, kwargs=kwargs, open=True)
        pools.append(pool)
        return pool

    yield make
    for pool in pools:
        pool.close()

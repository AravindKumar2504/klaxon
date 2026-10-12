-- The live patient's tables. The official PostgreSQL image runs this once, when
-- the volume is first created; the integration tests run it in a scratch
-- database. Synthetic data only. The roles come from 10-roles.sh.

CREATE SCHEMA ledger;
CREATE SCHEMA payments;
REVOKE ALL ON SCHEMA ledger, payments FROM PUBLIC;

-- Ledger -------------------------------------------------------------------
-- The first piece's tables (src/klaxon/patient/ledger.py), with the same names
-- and columns, so the money check runs unchanged on both.

CREATE TABLE ledger.accounts (
    name        text PRIMARY KEY,
    normal_side text NOT NULL CHECK (normal_side IN ('debit', 'credit'))
);
INSERT INTO ledger.accounts (name, normal_side) VALUES
    ('provider_clearing', 'debit'),  -- money the provider holds for us
    ('sales', 'credit'),
    ('refunds', 'debit');

-- event_id is deliberately NOT unique here: the ledger records what the
-- handler tells it to, which is how the naive handler can post twice.
CREATE TABLE ledger.transactions (
    id                      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id                text NOT NULL,
    payment_id              text NOT NULL,
    description             text NOT NULL,
    posted_at               timestamptz NOT NULL DEFAULT now(),
    source                  text,  -- the Kafka message it came from, e.g. provider-events/1@42
    reverses_transaction_id bigint UNIQUE REFERENCES ledger.transactions (id)  -- at most one reversal each
);
CREATE INDEX transactions_event_id ON ledger.transactions (event_id);
CREATE INDEX transactions_payment_id ON ledger.transactions (payment_id);

CREATE TABLE ledger.entries (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    transaction_id bigint NOT NULL REFERENCES ledger.transactions (id),
    account        text NOT NULL REFERENCES ledger.accounts (name),
    side           text NOT NULL CHECK (side IN ('debit', 'credit')),
    amount_cents   bigint NOT NULL CHECK (amount_cents > 0)
);
CREATE INDEX entries_transaction_id ON ledger.entries (transaction_id);

-- Written only by the idempotent handler. The primary key is the dedupe.
CREATE TABLE ledger.processed_events (
    event_id     text PRIMARY KEY,
    processed_at timestamptz NOT NULL DEFAULT now()
);

-- Payments -----------------------------------------------------------------
-- Our own record of what clients asked for. The money is booked in the ledger,
-- from the provider's webhooks, never from here. A payment's status comes from
-- the provider's answer to the payments API, or, when that answer is lost,
-- from the provider's webhook (the payment confirmer).

-- MAXVALUE keeps the six-digit ids from ever being cut short. A number is used up only when a new
-- row is inserted (the store looks a key up before inserting), and past 999,999 inserts fail loudly.
CREATE SEQUENCE payments.payment_number MAXVALUE 999999;
CREATE SEQUENCE payments.refund_number MAXVALUE 999999;

CREATE TABLE payments.payments (
    id                  text PRIMARY KEY DEFAULT 'pmt_' || lpad(nextval('payments.payment_number')::text, 6, '0'),
    idempotency_key     text NOT NULL UNIQUE,  -- the database's guarantee behind Redis
    request_fingerprint text NOT NULL,
    customer            text NOT NULL,
    amount_cents        bigint NOT NULL CHECK (amount_cents > 0),
    currency            text NOT NULL CHECK (currency = 'usd'),
    status              text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    provider_payment_id text UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payments.refunds (
    id                  text PRIMARY KEY DEFAULT 'rfd_' || lpad(nextval('payments.refund_number')::text, 6, '0'),
    payment_id          text NOT NULL REFERENCES payments.payments (id),
    idempotency_key     text NOT NULL UNIQUE,
    request_fingerprint text NOT NULL,
    amount_cents        bigint NOT NULL CHECK (amount_cents > 0),
    status              text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    provider_refund_id  text UNIQUE,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX refunds_payment_id ON payments.refunds (payment_id);

-- Who may do what. Nothing is granted to PUBLIC. -----------------------------

-- New databases let PUBLIC create temporary tables; no service needs that, least of all the read-only one.
DO $$ BEGIN
    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;

GRANT USAGE ON SCHEMA ledger TO ledger_writer, klaxon_readonly;
GRANT USAGE ON SCHEMA payments TO payments_api, payment_confirmer, klaxon_readonly;

-- The ledger consumer: read and add. No UPDATE, no DELETE: ledger rows are
-- never edited or deleted, and a wrong posting is repaired by a reversal.
GRANT SELECT ON ledger.accounts TO ledger_writer;
GRANT SELECT, INSERT ON ledger.transactions, ledger.entries, ledger.processed_events TO ledger_writer;

-- The payments API: its own tables. It may change a payment's status, never its amount.
GRANT SELECT, INSERT ON payments.payments, payments.refunds TO payments_api;
GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payments_api;
GRANT UPDATE (status, provider_refund_id, updated_at) ON payments.refunds TO payments_api;
GRANT USAGE ON SEQUENCE payments.payment_number, payments.refund_number TO payments_api;

-- The payment confirmer: marks payments succeeded from the provider's webhooks.
-- It reads the four columns it compares (not customers or idempotency keys)
-- and may change a status and the provider's id, nothing else: no new rows,
-- no amounts, no refunds, no ledger.
GRANT SELECT (id, status, amount_cents, provider_payment_id) ON payments.payments TO payment_confirmer;
GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payment_confirmer;

-- Klaxon and the money check: read everything, write nothing.
GRANT SELECT ON ALL TABLES IN SCHEMA ledger, payments TO klaxon_readonly;

#!/bin/bash
# Runs once, when the PostgreSQL volume is first created (the official image's
# /docker-entrypoint-initdb.d convention). One login role per service, each
# allowed only what that service needs; the grants are in 20-schema.sql.
# Local development passwords, set in compose.yaml. Not secrets.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v ledger_writer_password="$LEDGER_WRITER_PASSWORD" \
    -v payments_api_password="$PAYMENTS_API_PASSWORD" \
    -v payment_confirmer_password="$PAYMENT_CONFIRMER_PASSWORD" \
    -v klaxon_readonly_password="$KLAXON_READONLY_PASSWORD" <<'SQL'
-- The ledger consumer. Adds ledger rows; can never edit or delete them.
CREATE ROLE ledger_writer LOGIN PASSWORD :'ledger_writer_password';
ALTER ROLE ledger_writer SET search_path = ledger;

-- The payments API. Its own two tables only; no access to the ledger.
CREATE ROLE payments_api LOGIN PASSWORD :'payments_api_password';
ALTER ROLE payments_api SET search_path = payments;

-- The payment confirmer. Moves a payment's status from the provider's webhooks; no new rows, no ledger.
CREATE ROLE payment_confirmer LOGIN PASSWORD :'payment_confirmer_password';
ALTER ROLE payment_confirmer SET search_path = payments;

-- Klaxon and the money check. Reads everything, writes nothing. The grants are
-- the real guard; read-only transactions and a statement timeout are a second layer.
CREATE ROLE klaxon_readonly LOGIN PASSWORD :'klaxon_readonly_password' CONNECTION LIMIT 10;
ALTER ROLE klaxon_readonly SET search_path = ledger, payments;
ALTER ROLE klaxon_readonly SET default_transaction_read_only = on;
ALTER ROLE klaxon_readonly SET statement_timeout = '5s';
ALTER ROLE klaxon_readonly SET idle_in_transaction_session_timeout = '30s';
SQL

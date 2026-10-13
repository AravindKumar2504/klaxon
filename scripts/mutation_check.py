"""Break the code on purpose, one way at a time, on a copy of the repo, and check the tests notice.

A test suite that still passes when the code is broken proves nothing. Each "mutant" below
replaces one piece of code with a plausible mistake (commit before booking, answer 200 when
Kafka did not confirm, trust Redis alone, ...). The copy lives in WORKDIR, so the repo itself
is never touched. A mutant is "caught" when at least one test fails.

Usage: python scripts/mutation_check.py REPO WORKDIR [--with-stack]    (or: make mutants)
Offline mutants run the offline suite. --with-stack also runs the mutants that need the
stack's PostgreSQL, Redis and Kafka (pytest -m integration; make up first).
Exit 0 when every mutant is caught, 1 when one survives, 2 when the unbroken copy fails.
"""

import shutil
import subprocess
import sys
from pathlib import Path

S = "src/klaxon/patient/services/"

# (name, file, old, new, needs_stack)
MUTANTS = [
    ("consumer commits before booking", S + "consumer.py",
     "            if self.handle(msg, stop):\n                self._commit(msg)",
     "            self._commit(msg)\n            self.handle(msg, stop)", False),
    ("consumer gives up on a database outage and commits", S + "consumer.py",
     "            if stop.wait(delay):\n                return None  # shutting down",
     "            return \"gave_up\"\n            if stop.wait(delay):\n                return None  # shutting down", False),
    ("consumer dead-letters every database error", S + "consumer.py",
     "            except REFUSED_VALUES as exc:", "            except Exception as exc:", False),
    ("consumer retries refused values forever", S + "consumer.py",
     "REFUSED_VALUES = (psycopg.DataError, psycopg.IntegrityError, psycopg.errors.ProgramLimitExceeded, UnicodeError)",
     "REFUSED_VALUES = (psycopg.DataError,)", False),
    ("parser lets malformed ids through", S + "events.py",
     'ID = re.compile(r"[A-Za-z0-9_]{1,64}")', 'ID = re.compile(r"(?s).+")', False),
    ("parser crashes on a null body", S + "events.py",
     "    if not isinstance(body, (bytes, bytearray)):\n        raise BadEvent(\"no message body\")",
     "    if False:\n        raise BadEvent(\"no message body\")", False),
    ("parser crashes on deep nesting", S + "events.py",
     "    except RecursionError:\n        raise BadEvent(\"JSON nested too deeply\") from None\n", "", False),
    ("release deletes whoever holds the key", S + "idempotency.py",
     "            self.client.eval(RELEASE, 1, self._name(scope, key), claim.held)",
     "            self.client.delete(self._name(scope, key))", False),
    ("a crashed request keeps its key", S + "payments_api.py",
     "        except BaseException:\n            idempotency.release(scope, key, claim)\n            raise",
     "        except BaseException:\n            raise", False),
    ("logs record the raw path", S + "common.py",
     '    return getattr(request.scope.get("route"), "path", None) or "(no matching route)"',
     "    return request.url.path", False),
    ("logs keep long strings whole", S + "common.py",
     "    if isinstance(value, str) and len(value) > MAX_LOGGED_CHARS:", "    if False:", False),
    ("receiver answers any Host", S + "receiver_app.py", '    add_trusted_hosts(app, "webhook-receiver")\n', "", False),
    ("settling reads Kafka before the provider", "src/klaxon/checks/live.py",
     "    waiting = pending()\n    return waiting, lags()", "    behind = lags()\n    return pending(), behind", False),
    ("publisher keeps a dead producer", S + "kafka_io.py",
     "            self._producer = Producer(self._config)  # the poll thread picks up the new one", "            pass", False),
    ("consumer drops unbookable messages instead of dead-lettering", S + "consumer.py",
     'span, self._dead_letter(msg, source, f"cannot {self.verb} this event: {exc}", "bad_event", stop)',
     'span, "dropped"', False),
    ("receiver answers 200 when Kafka did not confirm", S + "receiver_app.py",
     '                return result("queue_unavailable", response)',
     '                return result("queued", JSONResponse({"received": True, "event_id": event_id}))', False),
    ("receiver skips the signature check", S + "receiver_app.py",
     "            verify_any(request.headers.get(signatures.HEADER), body, secrets, int(now()), tolerance_seconds)",
     "            pass", False),
    ("receiver accepts any timestamp", S + "receiver_app.py",
     "            verify_any(request.headers.get(signatures.HEADER), body, secrets, int(now()), tolerance_seconds)",
     "            verify_any(request.headers.get(signatures.HEADER), body, secrets, int(now()), 10**9)", False),
    ("receiver accepts only the first secret", S + "receiver_app.py",
     "    for secret in secrets:\n", "    for secret in secrets[:1]:\n", False),
    ("receiver publishes a re-encoded body, not the signed bytes", S + "receiver_app.py",
     "publisher.publish, topic, key, body, headers)", "publisher.publish, topic, key, body.strip() + b' ', headers)", False),
    ("provider treats a lost acknowledgement as delivered", S + "provider_delivery.py",
     "        if accepted and not ack_lost:", "        if accepted:", False),
    ("provider never retries a failed delivery", S + "provider_delivery.py",
     '        elif sent_at - delivery.created > MAX_AGE_SECONDS:', "        elif True:", False),
    ("provider replays a key for a different request", S + "provider_store.py",
     '                    if seen["endpoint"] != endpoint or seen["request_hash"] != request_hash:',
     "                    if False:", False),
    ("provider forgets the outbox row", S + "provider_store.py",
     '            "INSERT INTO deliveries (event_id, attempts, next_attempt_at) VALUES (?, 0, ?)", (event_id, self.clock())',
     '            "SELECT ?, ?", (event_id, self.clock())', False),
    ("redis claim without NX", S + "idempotency.py",
     "                if self.client.set(name, running, nx=True, ex=LOCK_TTL_SECONDS):",
     "                if self.client.set(name, running, ex=LOCK_TTL_SECONDS):", False),
    ("api marks an unknown provider outcome as failed and final", S + "payments_api.py",
     '                return refused(503, "provider_unavailable", message, final=False, payment=payment_view(row))',
     '                return refused(402, "payment_failed", message, final=True, payment=payment_view(row))', False),
    ("api uses a new provider key on every attempt", S + "payments_api.py",
     '                    row.customer, row.amount_cents, row.currency, row.id, metadata={"klaxon_payment_id": row.id}',
     '                    row.customer, row.amount_cents, row.currency, row.id + str(len(str(id(row)))) + str(id(row)), metadata={"klaxon_payment_id": row.id}',
     False),
    ("api trusts Redis only: a fresh database key per request", S + "payments_api.py",
     "                idempotency_key, request_fingerprint, payment.customer, payment.amount_cents, payment.currency",
     "                idempotency_key + str(id(payment)), request_fingerprint, payment.customer, payment.amount_cents, payment.currency",
     False),
    ("api skips the refund limit check", S + "payments_api.py",
     "                if refunded + row.amount_cents > payment.amount_cents:", "                if False:", False),
    ("provider client retries refusals", S + "provider_client.py",
     "                if response.status_code < 500 and response.status_code not in RETRYABLE_STATUSES:",
     "                if False:", False),
    ("provider client uses no idempotency key", S + "provider_client.py",
     'headers={"Idempotency-Key": idempotency_key}', "headers={}", False),
    ("events accept any currency", S + "events.py",
     "    if data.get(\"currency\") != CURRENCY:", "    if False:", False),
    ("settled after one quiet check", "src/klaxon/checks/live.py", "        if quiet >= 2:", "        if quiet >= 1:", False),
    ("settled while the consumer still lags", "src/klaxon/checks/live.py",
     "        quiet = quiet + 1 if pending == 0 and not any(behind.values()) else 0", "        quiet = quiet + 1 if pending == 0 else 0", False),
    # Week 2: payment confirmation and the fourth invariant.
    ("settling ignores the payment confirmer", S + "kafka_io.py",
     "CONSUMER_GROUPS = (LEDGER_GROUP, CONFIRMER_GROUP)", "CONSUMER_GROUPS = (LEDGER_GROUP,)", False),
    # Week 2: telemetry.
    ("server spans keep the raw path", S + "telemetry.py",
     'CALLER_CHOSEN = ("url.path", "url.query", "http.request.method_original", "server.address")',
     'CALLER_CHOSEN = ("url.query", "http.request.method_original", "server.address")', False),
    ("server spans are never redacted", S + "telemetry.py",
     "    tracer_provider.add_span_processor(RedactCallerText())\n", "", False),
    ("span attributes are not capped", S + "telemetry.py",
     "span_limits=SpanLimits(max_attribute_length=MAX_LOGGED_CHARS)", "span_limits=SpanLimits()", False),
    ("JSON lines lose their trace id", S + "common.py", "        if span.is_valid:", "        if False:", False),
    ("receiver does not pass the trace on to Kafka", S + "receiver_app.py",
     "            headers |= telemetry.kafka_trace_headers()  # traceparent: the consumers continue this trace",
     "            pass", False),
    ("consumer starts a new trace instead of continuing the webhook's", S + "consumer.py",
     "context=parent, kind=SpanKind.CONSUMER", "context=None, kind=SpanKind.CONSUMER", False),
    ("consumer outcomes are not counted", S + "consumer.py",
     '        EVENTS.add(1, {"consumer": self.name, "outcome": outcome})', "        pass", False),
    ("consumer retries are not counted", S + "consumer.py",
     '            RETRIES.add(1, {"consumer": self.name})', "            pass", False),
    ("webhook results are not counted", S + "receiver_app.py",
     '        WEBHOOKS.add(1, {"result": name})', "        pass", False),
    ("pending gauge counts stuck payments twice", S + "payments_api.py",
     'Observation(pending - stuck, {"age": "under_1m"})', 'Observation(pending, {"age": "under_1m"})', False),
    ("lag measuring keeps going after Kafka fails", S + "kafka_io.py",
     "            return out\n        out += [", "            continue\n        out += [", False),
    ("callers' trace context is trusted", S + "telemetry.py",
     "    propagate.set_global_textmap(IgnoreOutsideContext())  # trace context from outside is ignored\n", "", False),
    ("the Kafka trace takes tracestate and baggage too", S + "telemetry.py",
     '        if key != "traceparent":\n            return None', "        if False:\n            return None", False),
    ("health checks are traced", S + "telemetry.py",
     '    return scope.get("path") == "/healthz"', "    return False", False),
    ("FastAPI adds operation spans", S + "telemetry.py", '"operation_spans": False', '"operation_spans": True', False),
    ("the SDK's own log lines are forwarded", S + "telemetry.py",
     '        if record.name.startswith("opentelemetry"):', "        if False:", False),
    ("database durations get the SDK's millisecond buckets", S + "telemetry.py",
     "resource=resource, views=[db_durations])", "resource=resource)", False),
    ("consumers log the provider's id as payment_id", S + "consumer.py",
     "payment_id=event.klaxon_payment_id,  # ours", "payment_id=event.payment_id,  # ours", False),
    ("Related logs search only the span's own milliseconds", "docker/grafana/datasources.yaml",
     '        spanStartTimeShift: "-1m"\n        spanEndTimeShift: "1m"\n', "", False),
    ("a refund's span records the caller's path", S + "payments_api.py",
     "            payment = store.get_payment(payment_id)\n",
     "            trace.get_current_span().set_attribute(\"klaxon.payment_id\", payment_id)\n"
     "            payment = store.get_payment(payment_id)\n", False),
    ("the provider's id goes on a span unchecked", S + "payments_api.py",
     '            if ID.fullmatch(row.provider_payment_id or ""):', "            if True:", False),
    ("fourth invariant accepts a charged payment left pending", "src/klaxon/checks/money.py",
     '            if status != "succeeded":\n                problems.append(f"{status} but charged")',
     '            if False:\n                problems.append(f"{status} but charged")', False),
    ("fourth invariant misses a double charge", "src/klaxon/checks/money.py",
     "        if len(found) > 1:", "        if False:", False),
    ("fourth invariant ignores amounts", "src/klaxon/checks/money.py",
     "            if charge.amount_cents != amount:", "            if False:", False),
    ("fourth invariant stops at a payment's first problem", "src/klaxon/checks/money.py",
     "            if charge.amount_cents != amount:", "            elif charge.amount_cents != amount:", False),
    ("fourth invariant ignores our link to an unnamed charge", "src/klaxon/checks/money.py",
     "        elif charge.id in linked:", "        elif False:", False),
    ("fourth invariant accepts a succeeded payment never charged", "src/klaxon/checks/money.py",
     '                problems = ["succeeded but never charged"]', "                continue", False),
    ("fourth invariant ignores charges that are not ours", "src/klaxon/checks/money.py",
     "            strangers.append(charge)", "            pass", False),
    ("events leave out our payment id", S + "provider_store.py",
     '            payload["data"]["metadata"] = metadata', "            pass", False),
    ("parser trusts any id in the metadata", S + "events.py",
     "        klaxon_payment_id=ours if _is_id(ours) else None,", "        klaxon_payment_id=ours,", False),
    ("provider answers 200 after a charge it should fail", S + "provider_app.py",
     '        if sender is not None and sender.behavior.fails_after_charge(idempotency_key or body["id"]):',
     "        if False:", False),
    ("confirmer treats a missing payment of ours as nothing to do", S + "payment_confirmer.py",
     '        if mode == "by_provider_id":\n            return "not_found", None',
     '        if True:\n            return "not_found", None', False),
    ("confirmer calls a failed payment's charge something else", S + "payment_confirmer.py",
     '    if ours.status == "failed":', "    if False:", False),
    # Need the stack's PostgreSQL and Kafka:
    ("ledger posts a repeated event anyway", S + "pg_ledger.py", "                if inserted == 0:", "                if False:", True),
    ("dedupe row committed in its own transaction", S + "pg_ledger.py",
     "        with self.pool.connection() as conn, conn.transaction():\n            if self.mode == \"idempotent\":\n                inserted = conn.execute(",
     "        with self.pool.connection() as conn:\n            if self.mode == \"idempotent\":\n                inserted = conn.execute(",
     True),
    ("money check sum not cast to BIGINT", "src/klaxon/checks/money.py",
     "        \"SELECT CAST(COALESCE(SUM(CASE side WHEN 'debit' THEN amount_cents ELSE -amount_cents END), 0) AS BIGINT)\"",
     "        \"SELECT COALESCE(SUM(CASE side WHEN 'debit' THEN amount_cents ELSE -amount_cents END), 0)\"", True),
    ("ledger writer may UPDATE entries", "docker/postgres/initdb/20-schema.sql",
     "GRANT SELECT, INSERT ON ledger.transactions, ledger.entries, ledger.processed_events TO ledger_writer;",
     "GRANT SELECT, INSERT, UPDATE ON ledger.transactions, ledger.entries, ledger.processed_events TO ledger_writer;", True),
    ("Klaxon's role may write", "docker/postgres/initdb/20-schema.sql",
     "GRANT SELECT ON ALL TABLES IN SCHEMA ledger, payments TO klaxon_readonly;",
     "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA ledger, payments TO klaxon_readonly;", True),
    ("payments API may change amounts", "docker/postgres/initdb/20-schema.sql",
     "GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payments_api;",
     "GRANT UPDATE ON payments.payments TO payments_api;", True),
    ("store uses up an id on every repeat", S + "payments_store.py",
     "            row = conn.execute(\"SELECT * FROM payments WHERE idempotency_key = %s\", (idempotency_key,)).fetchone()\n            if row is not None:\n                return PaymentRow(**row), False\n            row = conn.execute(\n                \"INSERT INTO payments",
     "            row = conn.execute(\n                \"INSERT INTO payments", True),
    ("PUBLIC keeps temporary tables", "docker/postgres/initdb/20-schema.sql",
     "    EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());",
     "    PERFORM 1;", True),
    ("Lua release replaced by a plain delete (real Redis)", S + "idempotency.py",
     "            self.client.eval(RELEASE, 1, self._name(scope, key), claim.held)",
     "            self.client.delete(self._name(scope, key))", True),
    ("payments without a unique idempotency key", "docker/postgres/initdb/20-schema.sql",
     "    idempotency_key     text NOT NULL UNIQUE,  -- the database's guarantee behind Redis",
     "    idempotency_key     text NOT NULL,", True),
    ("confirmer looks payments up by the provider's id", S + "payment_confirmer.py",
     '            if self.mode == "by_our_id":\n                changed = conn.execute(',
     "            if False:\n                changed = conn.execute(", True),
    ("confirmer overwrites a final status", S + "payment_confirmer.py",
     "\" WHERE id = %(ours)s AND status = 'pending' AND amount_cents = %(amount)s\"",
     "\" WHERE id = %(ours)s AND amount_cents = %(amount)s\"", True),
    ("confirmer ignores the amount", S + "payment_confirmer.py",
     "\" WHERE id = %(ours)s AND status = 'pending' AND amount_cents = %(amount)s\"",
     "\" WHERE id = %(ours)s AND status = 'pending'\"", True),
    ("confirmer re-links a payment to another charge", S + "payment_confirmer.py",
     '                    "   AND (provider_payment_id IS NULL OR provider_payment_id = %(provider)s)",',
     '                    "",', True),
    ("confirmer lets a second link become a dead letter", S + "payment_confirmer.py",
     "        except psycopg.errors.UniqueViolation:", "        except psycopg.errors.QueryCanceled:", True),
    ("confirmer role may change amounts", "docker/postgres/initdb/20-schema.sql",
     "GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payment_confirmer;",
     "GRANT UPDATE ON payments.payments TO payment_confirmer;", True),
    ("confirmer role may add payments", "docker/postgres/initdb/20-schema.sql",
     "GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payment_confirmer;",
     "GRANT UPDATE (status, provider_payment_id, updated_at) ON payments.payments TO payment_confirmer;\n"
     "GRANT INSERT ON payments.payments TO payment_confirmer;", True),
    ("confirmer role may read every column", "docker/postgres/initdb/20-schema.sql",
     "GRANT SELECT (id, status, amount_cents, provider_payment_id) ON payments.payments TO payment_confirmer;",
     "GRANT SELECT ON payments.payments TO payment_confirmer;", True),
    ("the live check leaves out the fourth invariant", "src/klaxon/checks/live.py",
     "        return check_money(provider, conn, provider.payments)", "        return check_money(provider, conn)", True),
    ("pending count ignores age", S + "payments_store.py",
     "created_at < now() - make_interval(secs => %s)", "created_at < now() + make_interval(secs => %s)", True),
    ("lag treats a group with no commits as caught up", S + "kafka_io.py",
     "        lag[p] = end[p] - (place if place >= 0 else start[p])", "        lag[p] = end[p] - (place if place >= 0 else end[p])", True),
]


def run_tests(work: Path, python: str, integration: bool) -> subprocess.CompletedProcess:
    args = [python, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"]
    if integration:
        args += ["-m", "integration"]
    env = {"PYTHONPATH": str(work / "src"), "PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    return subprocess.run(args, cwd=work, env=env, capture_output=True, text=True, timeout=600)


def main() -> int:
    repo, work = Path(sys.argv[1]), Path(sys.argv[2])
    with_stack = "--with-stack" in sys.argv
    python = str(repo / ".venv" / "bin" / "python")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    for item in ("src", "tests", "docker", "compose.yaml", "pyproject.toml"):
        src = repo / item
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, work / item)

    check = subprocess.run(
        [python, "-c", "import klaxon, sys; print(klaxon.__file__)"],
        env={"PYTHONPATH": str(work / "src")}, capture_output=True, text=True,
    )
    assert str(work) in check.stdout, f"the copy is not the code under test: {check.stdout}"

    selected = [m for m in MUTANTS if with_stack or not m[4]]
    for integration in sorted({m[4] for m in selected}):
        baseline = run_tests(work, python, integration)
        if baseline.returncode != 0:
            print(f"baseline FAILED ({'integration' if integration else 'offline'}):\n{baseline.stdout[-3000:]}")
            return 2
        print(f"baseline passes ({'integration' if integration else 'offline'})")

    survived = []
    for name, file, old, new, integration in selected:
        path = work / file
        original = path.read_text()
        assert original.count(old) == 1, f"{name}: the text to break is not found exactly once in {file}"
        path.write_text(original.replace(old, new))
        try:
            result = run_tests(work, python, integration)
        finally:
            path.write_text(original)
        killed = result.returncode != 0
        failing = next((line for line in result.stdout.splitlines() if line.startswith("FAILED")), "")
        print(f"{'caught  ' if killed else 'SURVIVED'}  {name}  {failing[:150]}")
        if not killed:
            survived.append(name)
    print(f"\n{len(selected) - len(survived)} of {len(selected)} deliberate breaks caught")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())

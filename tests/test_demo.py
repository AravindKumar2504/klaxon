import json
from datetime import datetime, timezone

from klaxon.checks.money import check_money
from klaxon.demo import duplicate_webhook
from klaxon.demo.cli import main
from klaxon.patient.dashboard import summarize
from klaxon.patient.ledger import RequestRecord


def test_naive_run_exits_1_and_writes_labelled_evidence(tmp_path, capsys):
    assert main(["duplicate-webhook", "--handler", "naive", "--evidence-dir", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "SYNTHETIC, SEEDED DEMO" in out
    assert "It is not the agent." in out
    assert "status            GREEN" in out
    assert "RESULT: money check FAILED (dashboard GREEN, ledger balanced, money off by +$" in out

    [path] = tmp_path.glob("*_duplicate-webhook_naive_seed7.json")
    record = json.loads(path.read_text())
    assert record["label"].startswith("Synthetic, seeded demo.")
    assert record["money_check_passed"] is False
    assert record["dashboard"]["status"] == "GREEN"
    assert record["rerun_command"] == f"python -m klaxon.demo duplicate-webhook --handler naive --evidence-dir {tmp_path}"
    assert len(record["code_sha256"]) == 64
    [dup] = record["money_check"]["invariants"][0]["evidence"]
    assert dup["times_posted"] == 2
    assert record["money_check"]["money_off_cents"] == dup["amount_cents"]


def test_idempotent_run_exits_0(tmp_path, capsys):
    assert main(["duplicate-webhook", "--handler", "idempotent", "--evidence-dir", str(tmp_path)]) == 0
    assert "RESULT: money check PASSED" in capsys.readouterr().out


def test_no_evidence_flag_writes_nothing(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["duplicate-webhook", "--handler", "naive", "--no-evidence"]) == 1
    assert list(tmp_path.iterdir()) == []


def test_same_seed_gives_the_same_story():
    a, b = duplicate_webhook.run("naive", seed=11), duplicate_webhook.run("naive", seed=11)
    assert duplicate_webhook.render(a) == duplicate_webhook.render(b)
    assert a.report == b.report


def test_evidence_files_never_overwrite_each_other(tmp_path):
    result = duplicate_webhook.run("idempotent")
    when = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
    first = duplicate_webhook.write_evidence(result, tmp_path, "cmd", now=when)
    second = duplicate_webhook.write_evidence(result, tmp_path, "cmd", now=when)
    assert first != second
    assert first.exists() and second.exists()


def test_a_red_dashboard_needs_more_than_one_percent_failures():
    ok = [RequestRecord("e", 200, "posted")] * 199
    assert summarize(ok + [RequestRecord(None, 400, "bad_signature")]).status == "GREEN"  # 0.5%
    assert summarize(ok[:99] + [RequestRecord(None, 500, "error")]).status == "GREEN"  # exactly 1%
    assert summarize(ok[:98] + [RequestRecord(None, 500, "error")] * 2).status == "RED"  # 2%
    assert summarize([]).status == "GREEN"


def test_client_errors_count_against_the_dashboard_too():
    # A 400 (say, a bad signature) means a webhook was not accepted, so it is an error.
    dashboard = summarize([RequestRecord("e", 200, "posted"), RequestRecord(None, 400, "bad_signature")])
    assert dashboard.client_errors == 1
    assert dashboard.error_rate == 0.5
    assert dashboard.status == "RED"


def test_a_failure_with_agreeing_totals_says_which_invariant_failed():
    result = duplicate_webhook.run("idempotent")
    result.ledger.conn.execute("UPDATE transactions SET payment_id = 'pay_0002' WHERE event_id = 'evt_0001'")
    result.report = check_money(result.provider, result.ledger.conn)
    assert duplicate_webhook.verdict(result).endswith("totals agree, but failed: matches_provider)")

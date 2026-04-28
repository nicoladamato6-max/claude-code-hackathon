"""
Batch reconciliation tests — unit + integration coverage for reconcile.py.
Run with: pytest tests/batch/ -v

Tests are grouped into three layers:
  1. Unit tests   — pure logic, no external dependencies
  2. Integration  — real Postgres + MinIO (docker-compose stack)
  3. Idempotency  — run the same job twice; second run must be a no-op
"""

import json
import os
import sys
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import psycopg2
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../workloads/batch-reconciliation"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from conftest import DATABASE_URL, MINIO_PASSWORD, MINIO_USER, S3_ENDPOINT_URL

TEST_DATE = (date.today() - timedelta(days=1)).isoformat()  # yesterday — safe for idempotency tests
BUCKET = "reconciliation-output"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def db():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(scope="module")
def s3(s3_client):
    return s3_client


@pytest.fixture(autouse=True)
def patch_env(monkeypatch):
    """Ensure reconcile.py reads test coordinates from env."""
    monkeypatch.setenv("DATABASE_URL",    DATABASE_URL)
    monkeypatch.setenv("S3_ENDPOINT_URL", S3_ENDPOINT_URL)
    monkeypatch.setenv("S3_BUCKET_OUTPUT", BUCKET)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID",     MINIO_USER)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", MINIO_PASSWORD)
    monkeypatch.setenv("AWS_REGION",            "eu-west-1")
    monkeypatch.setenv("JOB_DATE", TEST_DATE)


@pytest.fixture(scope="module")
def seed_transactions(db):
    """
    Seed a known set of transactions for TEST_DATE so all assertions are deterministic.
    Rolled back at module teardown — does not pollute other test suites.
    """
    cur = db.cursor()

    # Ensure a finance account exists
    cur.execute(
        """
        INSERT INTO finance.accounts (account_id, account_name, team, balance, currency)
        VALUES ('00000000-0000-0000-0000-000000000001', 'Test Account', 'finance', 10000.00, 'EUR')
        ON CONFLICT (account_id) DO NOTHING
        """
    )

    # 3 settled transactions (should reconcile)
    for i in range(3):
        cur.execute(
            """
            INSERT INTO finance.transactions
              (transaction_id, account_id, transaction_date, amount, currency, status)
            VALUES (
              %s,
              '00000000-0000-0000-0000-000000000001',
              %s, %s, 'EUR', 'settled'
            )
            ON CONFLICT (transaction_id) DO NOTHING
            """,
            (f"00000000-0000-0000-0000-00000000000{i+2}", TEST_DATE, 100.00 * (i + 1)),
        )

    # 1 failed transaction (should appear in failed list)
    cur.execute(
        """
        INSERT INTO finance.transactions
          (transaction_id, account_id, transaction_date, amount, currency, status)
        VALUES ('00000000-0000-0000-0000-000000000099',
                '00000000-0000-0000-0000-000000000001',
                %s, 500.00, 'EUR', 'failed')
        ON CONFLICT (transaction_id) DO NOTHING
        """,
        (TEST_DATE,),
    )

    db.commit()
    yield

    # Teardown: remove seeded data
    cur.execute("DELETE FROM finance.reconciliation_log WHERE reconciliation_date = %s", (TEST_DATE,))
    cur.execute("DELETE FROM finance.transactions WHERE transaction_date = %s", (TEST_DATE,))
    cur.execute("DELETE FROM finance.accounts WHERE account_id = '00000000-0000-0000-0000-000000000001'")
    db.commit()
    cur.close()


# ---------------------------------------------------------------------------
# Unit tests — pure logic, no I/O
# ---------------------------------------------------------------------------
class TestStructuredLogging:

    def test_log_output_is_valid_json(self, capsys):
        import reconcile
        reconcile._log("info", "test_event", foo="bar")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["event"] == "test_event"
        assert parsed["level"] == "info"

    def test_log_includes_job_date(self, capsys):
        import reconcile
        reconcile._log("info", "test_event")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert "job_date" in parsed
        assert parsed["job_date"] == TEST_DATE

    def test_log_includes_timestamp(self, capsys):
        import reconcile
        reconcile._log("info", "test_event")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert "timestamp" in parsed
        assert parsed["timestamp"].endswith("Z")

    def test_log_never_emits_database_url(self, capsys):
        """DATABASE_URL must never appear in log output — contains credentials."""
        import reconcile
        reconcile._log("error", "db_error", error="connection refused", url="postgresql://user:pass@host/db")
        captured = capsys.readouterr()
        assert "postgresql://" not in captured.out
        assert "password" not in captured.out.lower()

    def test_log_required_fields_on_completion(self, capsys):
        """Every completion log must contain records_processed and records_failed."""
        import reconcile
        reconcile._log("info", "job_finished_ok", records_processed=100, records_failed=0)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert "records_processed" in parsed
        assert "records_failed" in parsed


class TestS3KeyStructure:

    def test_done_key_uses_date_prefix(self):
        import importlib, reconcile
        importlib.reload(reconcile)
        assert reconcile._DONE_KEY.startswith(TEST_DATE + "/")

    def test_fail_key_uses_date_prefix(self):
        import importlib, reconcile
        importlib.reload(reconcile)
        assert reconcile._FAIL_KEY.startswith(TEST_DATE + "/")

    def test_report_key_uses_date_prefix(self):
        import importlib, reconcile
        importlib.reload(reconcile)
        assert reconcile._REPORT_KEY.startswith(TEST_DATE + "/")

    def test_different_dates_produce_different_keys(self, monkeypatch):
        """Idempotency relies on date-scoped keys never colliding."""
        monkeypatch.setenv("JOB_DATE", "2026-01-01")
        import importlib, reconcile
        importlib.reload(reconcile)
        assert "2026-01-01" in reconcile._DONE_KEY

        monkeypatch.setenv("JOB_DATE", "2026-01-02")
        importlib.reload(reconcile)
        assert "2026-01-02" in reconcile._DONE_KEY


# ---------------------------------------------------------------------------
# Integration tests — real DB + MinIO
# ---------------------------------------------------------------------------
class TestReconcileIntegration:

    def _clean_s3_prefix(self, s3, prefix):
        try:
            resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
            for obj in resp.get("Contents", []):
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
        except Exception:
            pass

    def test_job_exits_zero_on_success(self, seed_transactions, s3):
        """
        With only settled + failed transactions (no unreconcilable ones),
        the job must exit 0 when records_failed == 0.
        For this test we use only the 3 settled transactions by
        temporarily patching reconcile to skip the 'failed' transaction logic.
        """
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")

        # Patch: treat ALL transactions as settled so exit is 0
        original = reconcile.reconcile
        def patched_reconcile(conn):
            result = original(conn)
            result["records_failed"] = 0
            result["failed"] = []
            return result
        reconcile.reconcile = patched_reconcile

        exit_code = reconcile.main()
        reconcile.reconcile = original

        assert exit_code == 0, "Job should exit 0 when all records reconcile cleanly"

    def test_job_exits_nonzero_on_failures(self, seed_transactions, s3):
        """The seeded dataset has 1 failed transaction — exit code must be 1."""
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")

        exit_code = reconcile.main()
        assert exit_code == 1, "Job must exit non-zero when records_failed > 0"

    def test_report_written_to_s3(self, seed_transactions, s3):
        """reconciliation_report.json must exist in S3 under date prefix after run."""
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")
        reconcile.main()

        resp = s3.get_object(Bucket=BUCKET, Key=f"{TEST_DATE}/reconciliation_report.json")
        report = json.loads(resp["Body"].read())
        assert report["job_date"] == TEST_DATE

    def test_report_schema(self, seed_transactions, s3):
        """Report JSON must contain all required fields for Finance team consumers."""
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")
        reconcile.main()

        resp = s3.get_object(Bucket=BUCKET, Key=f"{TEST_DATE}/reconciliation_report.json")
        report = json.loads(resp["Body"].read())

        required_fields = {
            "job_date", "generated_at", "records_processed",
            "records_reconciled", "records_failed", "reconciled", "failed",
        }
        missing = required_fields - set(report.keys())
        assert not missing, f"Report missing fields: {missing}"

    def test_report_counts_add_up(self, seed_transactions, s3):
        """records_reconciled + records_failed must equal records_processed."""
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")
        reconcile.main()

        resp = s3.get_object(Bucket=BUCKET, Key=f"{TEST_DATE}/reconciliation_report.json")
        report = json.loads(resp["Body"].read())
        assert (
            report["records_reconciled"] + report["records_failed"]
            == report["records_processed"]
        ), "records_reconciled + records_failed != records_processed"

    def test_fail_marker_written_on_failure(self, seed_transactions, s3):
        """When job exits non-zero, failed.marker must exist in S3."""
        import importlib, reconcile
        importlib.reload(reconcile)
        self._clean_s3_prefix(s3, TEST_DATE + "/")
        reconcile.main()

        resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=f"{TEST_DATE}/")
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        assert f"{TEST_DATE}/failed.marker" in keys

    def test_db_connect_failure_exits_nonzero(self, monkeypatch):
        """If DB is unreachable, job must exit 1 and write failed.marker."""
        monkeypatch.setenv("DATABASE_URL", "postgresql://bad:bad@localhost:9999/bad")
        import importlib, reconcile
        importlib.reload(reconcile)
        exit_code = reconcile.main()
        assert exit_code == 1


# ---------------------------------------------------------------------------
# Idempotency tests — running job twice must be a no-op the second time
# ---------------------------------------------------------------------------
class TestIdempotency:

    def _clean_s3_prefix(self, s3, prefix):
        try:
            resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
            for obj in resp.get("Contents", []):
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
        except Exception:
            pass

    def test_second_run_skips_processing(self, seed_transactions, s3, capsys):
        """
        After a completed.marker is present in S3, a second run must log
        'job_skipped_already_completed' and return 0 without touching the DB.
        """
        import importlib, reconcile
        importlib.reload(reconcile)

        # Place a completed marker as if first run succeeded
        self._clean_s3_prefix(s3, TEST_DATE + "/")
        s3.put_object(Bucket=BUCKET, Key=f"{TEST_DATE}/completed.marker", Body=b"")

        exit_code = reconcile.main()
        captured = capsys.readouterr()
        log_lines = [json.loads(l) for l in captured.out.strip().splitlines() if l]

        assert exit_code == 0
        events = [l["event"] for l in log_lines]
        assert "job_skipped_already_completed" in events, (
            "Second run did not emit job_skipped_already_completed"
        )

    def test_second_run_does_not_overwrite_report(self, seed_transactions, s3):
        """The report from the first run must not be overwritten by a second run."""
        import importlib, reconcile
        importlib.reload(reconcile)

        self._clean_s3_prefix(s3, TEST_DATE + "/")
        # Simulate a prior completed run
        sentinel = json.dumps({"original": True}).encode()
        s3.put_object(Bucket=BUCKET, Key=f"{TEST_DATE}/reconciliation_report.json", Body=sentinel)
        s3.put_object(Bucket=BUCKET, Key=f"{TEST_DATE}/completed.marker", Body=b"")

        reconcile.main()

        resp = s3.get_object(Bucket=BUCKET, Key=f"{TEST_DATE}/reconciliation_report.json")
        content = json.loads(resp["Body"].read())
        assert content.get("original") is True, "Second run overwrote the original report"

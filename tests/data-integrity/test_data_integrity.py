"""
Data integrity tests — verify data survived migration intact.
Run with: pytest tests/data-integrity/ -v

Requires SOURCE_DB_URL (on-prem) and TARGET_DB_URL (RDS) to be set.
Tests that need both DBs are skipped when SOURCE_DB_URL is unset
(safe to run in local docker-compose where only TARGET_DB_URL exists).
"""

import hashlib
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import pytest
from conftest import DATABASE_URL

SOURCE_DB_URL = os.environ.get("SOURCE_DB_URL")
TARGET_DB_URL = os.environ.get("TARGET_DB_URL", DATABASE_URL)

needs_both_dbs = pytest.mark.skipif(
    SOURCE_DB_URL is None,
    reason="SOURCE_DB_URL not set — skipping cross-DB integrity checks"
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def target_conn():
    conn = psycopg2.connect(TARGET_DB_URL, connect_timeout=10)
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def source_conn():
    if SOURCE_DB_URL is None:
        pytest.skip("SOURCE_DB_URL not set")
    conn = psycopg2.connect(SOURCE_DB_URL, connect_timeout=10)
    yield conn
    conn.close()


def row_count(conn, table: str) -> int:
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    result = cur.fetchone()[0]
    cur.close()
    return result


def table_checksum(conn, table: str, order_by: str) -> str:
    """MD5 of the ordered, concatenated row data — detects silent data corruption."""
    cur = conn.cursor()
    cur.execute(f"SELECT md5(string_agg(t::text, '|' ORDER BY {order_by})) FROM {table} t")
    result = cur.fetchone()[0]
    cur.close()
    return result or ""


# ---------------------------------------------------------------------------
# Schema structure tests (target DB only)
# ---------------------------------------------------------------------------
EXPECTED_SCHEMAS = ["app", "finance", "risk", "compliance", "ops", "exec"]

EXPECTED_COLUMNS = {
    "finance.transactions": {
        "transaction_id", "account_id", "transaction_date",
        "amount", "currency", "status", "description", "created_at",
    },
    "finance.accounts": {
        "account_id", "account_name", "team", "balance", "currency", "created_at",
    },
    "finance.reconciliation_log": {
        "id", "transaction_id", "reconciliation_date", "reconciled_amount", "reconciled_at",
    },
    "risk.positions": {
        "position_id", "asset_code", "quantity", "book_value",
        "market_value", "currency", "position_date", "created_at",
    },
    "compliance.audit_log": {
        "log_id", "event_type", "actor", "target_id", "details", "occurred_at",
    },
    "app.users": {
        "id", "username", "password_hash", "team", "created_at", "deactivated_at",
    },
}

EXPECTED_MATVIEWS = [
    ("finance", "mv_monthly_close"),
    ("risk",    "mv_var_daily"),
]

EXPECTED_PGCRON_JOBS = ["refresh-var-daily", "refresh-monthly-close"]


class TestSchemaStructure:

    @pytest.mark.parametrize("schema", EXPECTED_SCHEMAS)
    def test_schema_exists(self, schema, target_conn):
        cur = target_conn.cursor()
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
        assert cur.fetchone(), f"Schema '{schema}' missing on target"
        cur.close()

    @pytest.mark.parametrize("table,expected_cols", EXPECTED_COLUMNS.items())
    def test_table_columns(self, table, expected_cols, target_conn):
        schema, tbl = table.split(".")
        cur = target_conn.cursor()
        cur.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = %s AND table_name = %s",
            (schema, tbl),
        )
        actual_cols = {r[0] for r in cur.fetchall()}
        cur.close()
        missing = expected_cols - actual_cols
        assert not missing, f"{table}: missing columns {missing}"

    @pytest.mark.parametrize("schema,view", EXPECTED_MATVIEWS)
    def test_materialized_view_exists(self, schema, view, target_conn):
        cur = target_conn.cursor()
        cur.execute(
            "SELECT 1 FROM pg_matviews WHERE schemaname = %s AND matviewname = %s",
            (schema, view),
        )
        assert cur.fetchone(), f"Materialized view '{schema}.{view}' missing"
        cur.close()

    def test_exec_view_exists(self, target_conn):
        cur = target_conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.views"
            " WHERE table_schema = 'exec' AND table_name = 'v_executive_summary'"
        )
        assert cur.fetchone(), "exec.v_executive_summary view missing"
        cur.close()

    def test_exec_view_has_no_dblink(self, target_conn):
        """The dblink credential leak (discovery §3) must be fully removed."""
        cur = target_conn.cursor()
        cur.execute(
            "SELECT view_definition FROM information_schema.views"
            " WHERE table_schema = 'exec' AND table_name = 'v_executive_summary'"
        )
        row = cur.fetchone()
        cur.close()
        assert row, "exec.v_executive_summary not found"
        assert "dblink" not in row[0].lower(), "dblink still present in v_executive_summary"

    def test_no_security_definer_functions(self, target_conn):
        """Discovery §6: all three stored procedures rewritten to SECURITY INVOKER in V2."""
        cur = target_conn.cursor()
        cur.execute(
            "SELECT routine_schema, routine_name FROM information_schema.routines"
            " WHERE routine_type = 'FUNCTION' AND security_type = 'DEFINER'"
            "   AND routine_schema NOT IN ('pg_catalog', 'information_schema', 'cron')"
        )
        violations = cur.fetchall()
        cur.close()
        assert violations == [], f"SECURITY DEFINER functions still present: {violations}"

    def test_pgaudit_is_active(self, target_conn):
        """GDPR art.32: query-level audit log must be enabled."""
        cur = target_conn.cursor()
        cur.execute("SELECT current_setting('pgaudit.log', true)")
        setting = cur.fetchone()[0]
        cur.close()
        assert setting and setting != "", "pgaudit.log is not configured"

    @pytest.mark.parametrize("job_name", EXPECTED_PGCRON_JOBS)
    def test_pgcron_job_registered(self, job_name, target_conn):
        """Discovery §6 + SRE concern #3: mat-view refresh via pg_cron, not host cron."""
        cur = target_conn.cursor()
        cur.execute("SELECT 1 FROM cron.job WHERE jobname = %s", (job_name,))
        assert cur.fetchone(), f"pg_cron job '{job_name}' not registered"
        cur.close()

    @pytest.mark.parametrize("role", ["finance_ro", "risk_ro", "compliance_ro", "ops_ro", "exec_ro"])
    def test_per_team_role_exists(self, role, target_conn):
        """Discovery §4: shared reporting_user replaced by per-team read-only roles (V3)."""
        cur = target_conn.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        assert cur.fetchone(), f"Role '{role}' not found"
        cur.close()


# ---------------------------------------------------------------------------
# Row count parity (source vs target)
# ---------------------------------------------------------------------------
MIGRATION_TABLES = [
    "finance.transactions",
    "finance.accounts",
    "finance.reconciliation_log",
    "risk.positions",
    "compliance.audit_log",
    "ops.service_events",
]


class TestRowCounts:

    @needs_both_dbs
    @pytest.mark.parametrize("table", MIGRATION_TABLES)
    def test_row_count_matches(self, table, source_conn, target_conn):
        src = row_count(source_conn, table)
        tgt = row_count(target_conn, table)
        assert src == tgt, f"{table}: source={src} rows, target={tgt} rows — delta={abs(src - tgt)}"

    def test_users_table_not_empty(self, target_conn):
        count = row_count(target_conn, "app.users")
        assert count >= 5, f"app.users has {count} rows; expected at least 5 seed users"


# ---------------------------------------------------------------------------
# Data checksum tests (detect silent corruption)
# ---------------------------------------------------------------------------
class TestChecksums:

    @needs_both_dbs
    def test_transactions_checksum(self, source_conn, target_conn):
        src = table_checksum(source_conn, "finance.transactions", "transaction_id")
        tgt = table_checksum(target_conn, "finance.transactions", "transaction_id")
        assert src == tgt, "finance.transactions checksum mismatch — data may be corrupted"

    @needs_both_dbs
    def test_positions_checksum(self, source_conn, target_conn):
        src = table_checksum(source_conn, "risk.positions", "position_id")
        tgt = table_checksum(target_conn, "risk.positions", "position_id")
        assert src == tgt, "risk.positions checksum mismatch"

    @needs_both_dbs
    def test_audit_log_checksum(self, source_conn, target_conn):
        src = table_checksum(source_conn, "compliance.audit_log", "log_id")
        tgt = table_checksum(target_conn, "compliance.audit_log", "log_id")
        assert src == tgt, "compliance.audit_log checksum mismatch"


# ---------------------------------------------------------------------------
# Business rule tests (target DB only)
# ---------------------------------------------------------------------------
class TestBusinessRules:

    def test_transaction_status_values_valid(self, target_conn):
        """All transaction statuses must be within the allowed set."""
        cur = target_conn.cursor()
        cur.execute(
            "SELECT DISTINCT status FROM finance.transactions"
            " WHERE status NOT IN ('pending','settled','failed','cancelled')"
        )
        invalid = cur.fetchall()
        cur.close()
        assert invalid == [], f"Invalid transaction statuses found: {invalid}"

    def test_no_null_transaction_ids(self, target_conn):
        cur = target_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM finance.transactions WHERE transaction_id IS NULL")
        count = cur.fetchone()[0]
        cur.close()
        assert count == 0, f"{count} transactions with NULL transaction_id"

    def test_no_null_account_ids_on_transactions(self, target_conn):
        cur = target_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM finance.transactions WHERE account_id IS NULL")
        count = cur.fetchone()[0]
        cur.close()
        assert count == 0, f"{count} transactions with NULL account_id"

    def test_reconciliation_log_unique_per_date(self, target_conn):
        """Idempotency invariant: one reconciliation_log row per (transaction, date)."""
        cur = target_conn.cursor()
        cur.execute(
            "SELECT transaction_id, reconciliation_date, COUNT(*)"
            " FROM finance.reconciliation_log"
            " GROUP BY 1, 2 HAVING COUNT(*) > 1"
        )
        dupes = cur.fetchall()
        cur.close()
        assert dupes == [], f"Duplicate reconciliation_log entries found: {dupes}"

    def test_account_balances_non_negative(self, target_conn):
        """Financial constraint: account balance must not be negative (overdraft not modelled)."""
        cur = target_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM finance.accounts WHERE balance < 0")
        count = cur.fetchone()[0]
        cur.close()
        assert count == 0, f"{count} accounts with negative balance"

    def test_positions_have_valid_dates(self, target_conn):
        cur = target_conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM risk.positions"
            " WHERE position_date > CURRENT_DATE OR position_date < '2000-01-01'"
        )
        count = cur.fetchone()[0]
        cur.close()
        assert count == 0, f"{count} positions with invalid dates"

    def test_users_passwords_are_hashed(self, target_conn):
        """Passwords must be bcrypt hashes — never plaintext (discovery §4)."""
        cur = target_conn.cursor()
        cur.execute("SELECT password_hash FROM app.users LIMIT 10")
        rows = cur.fetchall()
        cur.close()
        for (pw_hash,) in rows:
            assert pw_hash.startswith("$2"), (
                f"Password does not look like bcrypt hash: '{pw_hash[:10]}...'"
            )

    def test_postgres15_timestamp_behaviour(self, target_conn):
        """
        Discovery §6: to_timestamp() with timezone changed between PG13 and PG15.
        Verify that a known timestamp round-trips correctly in the new version.
        """
        cur = target_conn.cursor()
        cur.execute(
            "SELECT to_timestamp('2026-01-15 14:30:00', 'YYYY-MM-DD HH24:MI:SS')"
            " AT TIME ZONE 'UTC'"
        )
        result = cur.fetchone()[0]
        cur.close()
        assert str(result).startswith("2026-01-15"), (
            f"to_timestamp() returned unexpected value after PG15 upgrade: {result}"
        )

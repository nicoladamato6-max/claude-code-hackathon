"""
Smoke tests — verify every service is reachable and minimally operational.
Run with: pytest tests/smoke/ -v
These tests must pass before any contract or data-integrity tests are attempted.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import requests
import psycopg2
from conftest import WEB_APP_URL, DATABASE_URL, REDIS_URL, S3_ENDPOINT_URL

# ---------------------------------------------------------------------------
# Web-app
# ---------------------------------------------------------------------------
class TestWebAppSmoke:

    def test_healthz_reachable(self):
        resp = requests.get(f"{WEB_APP_URL}/healthz", timeout=5)
        assert resp.status_code == 200

    def test_healthz_returns_ok(self):
        resp = requests.get(f"{WEB_APP_URL}/healthz", timeout=5)
        assert resp.json()["status"] == "ok"

    def test_healthz_db_check_present(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "checks" in body
        assert body["checks"].get("db") == "ok"

    def test_healthz_redis_check_present(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "checks" in body
        assert body["checks"].get("redis") in ("ok", "degraded")

    def test_healthz_content_type_json(self):
        resp = requests.get(f"{WEB_APP_URL}/healthz", timeout=5)
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_debug_mode_off(self):
        """Flask debug mode must be off — exposes interactive debugger if on."""
        resp = requests.get(f"{WEB_APP_URL}/nonexistent-route-404", timeout=5)
        assert resp.status_code == 404
        # Debug mode would return an HTML Werkzeug debugger page
        assert "Traceback" not in resp.text
        assert "werkzeug" not in resp.text.lower()

    def test_unauthenticated_api_blocked(self):
        resp = requests.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
class TestPostgresSmoke:

    def test_connection(self, db_conn):
        cur = db_conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        cur.close()

    def test_version_is_15(self, db_conn):
        """Confirms PG13 → PG15 upgrade completed successfully."""
        cur = db_conn.cursor()
        cur.execute("SELECT current_setting('server_version_num')::int")
        version_num = cur.fetchone()[0]
        cur.close()
        assert version_num >= 150000, f"Expected PG15+, got version_num={version_num}"

    @pytest.mark.parametrize("schema", ["app", "finance", "risk", "compliance", "ops", "exec"])
    def test_schema_exists(self, schema, db_conn):
        cur = db_conn.cursor()
        cur.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,))
        assert cur.fetchone() is not None, f"Schema '{schema}' not found"
        cur.close()

    @pytest.mark.parametrize("table", [
        "app.users",
        "finance.accounts",
        "finance.transactions",
        "finance.reconciliation_log",
        "risk.positions",
        "compliance.audit_log",
        "ops.service_events",
    ])
    def test_table_exists(self, table, db_conn):
        schema, tbl = table.split(".")
        cur = db_conn.cursor()
        cur.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_schema = %s AND table_name = %s",
            (schema, tbl),
        )
        assert cur.fetchone() is not None, f"Table '{table}' not found"
        cur.close()

    def test_pgaudit_extension_installed(self, db_conn):
        """GDPR art.32 requires query-level audit logging via pgaudit."""
        cur = db_conn.cursor()
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgaudit'")
        assert cur.fetchone() is not None, "pgaudit extension not installed"
        cur.close()

    def test_pgcrypto_extension_installed(self, db_conn):
        cur = db_conn.cursor()
        cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgcrypto'")
        assert cur.fetchone() is not None
        cur.close()

    def test_no_security_definer_functions(self, db_conn):
        """All stored procedures must be SECURITY INVOKER (discovery §6 / V2 migration)."""
        cur = db_conn.cursor()
        cur.execute(
            """
            SELECT routine_schema, routine_name
            FROM information_schema.routines
            WHERE routine_type = 'FUNCTION'
              AND security_type = 'DEFINER'
              AND routine_schema NOT IN ('pg_catalog', 'information_schema')
            """
        )
        definer_funcs = cur.fetchall()
        cur.close()
        assert definer_funcs == [], (
            f"Found SECURITY DEFINER functions that must be rewritten: {definer_funcs}"
        )

    def test_seed_users_exist(self, db_conn):
        cur = db_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM app.users WHERE deactivated_at IS NULL")
        count = cur.fetchone()[0]
        cur.close()
        assert count >= 5, "Expected at least 5 active seed users (one per team)"


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------
class TestRedisSmoke:

    def test_ping(self, redis_client):
        assert redis_client.ping() is True

    def test_set_get(self, redis_client):
        """Verifies Redis can store and retrieve values — not just answer pings."""
        key = "smoke:test:key"
        redis_client.set(key, "ok", ex=10)
        assert redis_client.get(key) == b"ok"
        redis_client.delete(key)

    def test_expiry_works(self, redis_client):
        """Session expiry relies on Redis TTL functioning correctly."""
        key = "smoke:test:ttl"
        redis_client.set(key, "1", ex=1)
        import time; time.sleep(1.1)
        assert redis_client.get(key) is None


# ---------------------------------------------------------------------------
# S3 / MinIO
# ---------------------------------------------------------------------------
class TestS3Smoke:

    @pytest.mark.parametrize("bucket", ["web-assets", "reconciliation-output", "db-backups"])
    def test_bucket_exists(self, bucket, s3_client):
        buckets = [b["Name"] for b in s3_client.list_buckets()["Buckets"]]
        assert bucket in buckets, f"Bucket '{bucket}' not found"

    def test_reconciliation_output_writable(self, s3_client):
        """Batch job must be able to write to reconciliation-output (idempotency marker)."""
        s3_client.put_object(
            Bucket="reconciliation-output",
            Key="smoke-test/write-check.txt",
            Body=b"ok",
        )
        resp = s3_client.get_object(Bucket="reconciliation-output", Key="smoke-test/write-check.txt")
        assert resp["Body"].read() == b"ok"
        s3_client.delete_object(Bucket="reconciliation-output", Key="smoke-test/write-check.txt")

    def test_web_assets_readable(self, s3_client):
        s3_client.put_object(Bucket="web-assets", Key="smoke-test/asset.txt", Body=b"asset")
        resp = s3_client.get_object(Bucket="web-assets", Key="smoke-test/asset.txt")
        assert resp["Body"].read() == b"asset"
        s3_client.delete_object(Bucket="web-assets", Key="smoke-test/asset.txt")

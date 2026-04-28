"""
Contract tests — verify the API behaves exactly as consumers expect.
Run with: pytest tests/contract/ -v
These tests define the API contract; breaking them signals a regression.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import requests
from conftest import WEB_APP_URL, TEST_USER, TEST_PASSWORD


# ---------------------------------------------------------------------------
# GET /healthz
# ---------------------------------------------------------------------------
class TestHealthz:

    def test_returns_200(self):
        assert requests.get(f"{WEB_APP_URL}/healthz", timeout=5).status_code == 200

    def test_content_type_is_json(self):
        resp = requests.get(f"{WEB_APP_URL}/healthz", timeout=5)
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_body_has_status_field(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "status" in body

    def test_status_is_ok_string(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert body["status"] == "ok"

    def test_body_has_checks_object(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "checks" in body
        assert isinstance(body["checks"], dict)

    def test_checks_includes_db(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "db" in body["checks"]

    def test_checks_includes_redis(self):
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        assert "redis" in body["checks"]

    def test_no_extra_fields_leak_internals(self):
        """Healthz must not expose stack traces, DSNs, or internal config."""
        body = requests.get(f"{WEB_APP_URL}/healthz", timeout=5).json()
        text = str(body).lower()
        for forbidden in ("password", "secret", "token", "traceback", "exception"):
            assert forbidden not in text, f"Sensitive word '{forbidden}' leaked in /healthz"


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------
class TestLogin:

    def test_valid_credentials_returns_200(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.status_code == 200

    def test_valid_credentials_returns_ok_status(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.json()["status"] == "ok"

    def test_valid_credentials_returns_team(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        assert "team" in resp.json()
        assert resp.json()["team"] == "finance"

    def test_wrong_password_returns_401(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": "wrong-password"},
            timeout=5,
        )
        assert resp.status_code == 401

    def test_unknown_user_returns_401(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": "nobody", "password": "x"},
            timeout=5,
        )
        assert resp.status_code == 401

    def test_missing_username_returns_400(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.status_code == 400

    def test_missing_password_returns_400(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER},
            timeout=5,
        )
        assert resp.status_code == 400

    def test_empty_body_returns_400(self):
        resp = requests.post(f"{WEB_APP_URL}/login", json={}, timeout=5)
        assert resp.status_code == 400

    def test_wrong_credentials_response_does_not_reveal_reason(self):
        """Error message must not distinguish 'user not found' from 'wrong password'."""
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": "nobody", "password": "x"},
            timeout=5,
        ).json()
        error_msg = resp.get("error", "").lower()
        assert "not found" not in error_msg
        assert "user" not in error_msg      # must not say "user does not exist"
        assert "password" not in error_msg  # must not say "wrong password"

    def test_login_sets_session_cookie(self):
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.status_code == 200
        assert "session" in resp.cookies or len(resp.cookies) > 0


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------
class TestLogout:

    def test_logout_returns_200(self, auth_session):
        resp = auth_session.post(f"{WEB_APP_URL}/logout", timeout=5)
        assert resp.status_code == 200

    def test_logout_invalidates_session(self, auth_session):
        """After logout, previously protected endpoints must return 401."""
        auth_session.post(f"{WEB_APP_URL}/logout", timeout=5)
        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code == 401

    def test_logout_unauthenticated_is_harmless(self, anon_session):
        """Calling /logout without a session must not crash the server."""
        resp = anon_session.post(f"{WEB_APP_URL}/logout", timeout=5)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/accounts
# ---------------------------------------------------------------------------
class TestAccounts:

    def test_unauthenticated_returns_401(self, anon_session):
        resp = anon_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code == 401

    def test_authenticated_returns_200(self, auth_session):
        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code == 200

    def test_returns_list(self, auth_session):
        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert isinstance(resp.json(), list)

    def test_account_schema(self, auth_session, db_conn):
        """Seed at least one account so the schema can be verified."""
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO finance.accounts (account_name, team, balance, currency)"
            " VALUES ('Test Account', 'finance', 1000.00, 'EUR')"
            " ON CONFLICT DO NOTHING"
        )
        db_conn.commit()
        cur.close()

        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        accounts = resp.json()
        if accounts:
            for field in ("account_id", "account_name", "balance", "currency"):
                assert field in accounts[0], f"Field '{field}' missing from account object"

    def test_balance_is_numeric(self, auth_session, db_conn):
        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        for account in resp.json():
            assert isinstance(account["balance"], (int, float))

    def test_content_type_is_json(self, auth_session):
        resp = auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_team_isolation(self):
        """risk.user must not see finance team accounts."""
        s = requests.Session()
        s.post(f"{WEB_APP_URL}/login", json={"username": "risk.user", "password": TEST_PASSWORD}, timeout=5)
        risk_accounts = s.get(f"{WEB_APP_URL}/api/accounts", timeout=5).json()
        s.post(f"{WEB_APP_URL}/logout", timeout=5)

        s2 = requests.Session()
        s2.post(f"{WEB_APP_URL}/login", json={"username": "finance.user", "password": TEST_PASSWORD}, timeout=5)
        finance_accounts = s2.get(f"{WEB_APP_URL}/api/accounts", timeout=5).json()
        s2.post(f"{WEB_APP_URL}/logout", timeout=5)

        risk_ids = {a["account_id"] for a in risk_accounts}
        finance_ids = {a["account_id"] for a in finance_accounts}
        assert risk_ids.isdisjoint(finance_ids), "Team isolation violated: shared accounts returned"


# ---------------------------------------------------------------------------
# GET /api/assets/<key>
# ---------------------------------------------------------------------------
class TestAssets:

    def test_unauthenticated_returns_401(self, anon_session):
        resp = anon_session.get(f"{WEB_APP_URL}/api/assets/logo.png", timeout=5)
        assert resp.status_code == 401

    def test_authenticated_returns_url(self, auth_session, s3_client):
        """Upload a test asset, then verify presigned URL is returned."""
        s3_client.put_object(Bucket="web-assets", Key="test/logo.png", Body=b"fake-png")
        resp = auth_session.get(f"{WEB_APP_URL}/api/assets/test/logo.png", timeout=5)
        assert resp.status_code == 200
        body = resp.json()
        assert "url" in body
        assert "logo.png" in body["url"]
        s3_client.delete_object(Bucket="web-assets", Key="test/logo.png")

    def test_missing_asset_returns_503_or_url(self, auth_session):
        """Non-existent asset: presign still works (S3 returns 403/404 when URL is fetched)."""
        resp = auth_session.get(f"{WEB_APP_URL}/api/assets/nonexistent.png", timeout=5)
        # Presigned URL generation succeeds for any key; the 404 is at fetch time
        assert resp.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Security response headers
# ---------------------------------------------------------------------------
class TestSecurityHeaders:

    def test_no_server_header_leaks_framework(self):
        resp = requests.get(f"{WEB_APP_URL}/healthz", timeout=5)
        server = resp.headers.get("Server", "")
        assert "werkzeug" not in server.lower(), "Werkzeug version exposed in Server header"

    def test_404_does_not_leak_stack_trace(self):
        resp = requests.get(f"{WEB_APP_URL}/this-does-not-exist", timeout=5)
        assert resp.status_code == 404
        assert "Traceback" not in resp.text


# ---------------------------------------------------------------------------
# Security tests — injection, cookie hardening
# ---------------------------------------------------------------------------
class TestSecurity:

    def test_sql_injection_in_login_username(self):
        """SQL injection in username must not return 200 or expose DB errors."""
        payloads = [
            "' OR '1'='1",
            "admin'--",
            "' UNION SELECT 1,2,3--",
            "'; DROP TABLE app.users;--",
        ]
        for payload in payloads:
            resp = requests.post(
                f"{WEB_APP_URL}/login",
                json={"username": payload, "password": "x"},
                timeout=5,
            )
            assert resp.status_code in (400, 401), (
                f"SQLi payload '{payload}' returned {resp.status_code} — possible injection"
            )
            assert "syntax" not in resp.text.lower(), "DB syntax error exposed"
            assert "psycopg" not in resp.text.lower(), "DB driver name exposed"

    def test_sql_injection_in_login_password(self):
        payloads = ["' OR '1'='1", "x' OR 1=1--"]
        for payload in payloads:
            resp = requests.post(
                f"{WEB_APP_URL}/login",
                json={"username": "finance.user", "password": payload},
                timeout=5,
            )
            assert resp.status_code == 401, (
                f"SQLi in password returned {resp.status_code} — possible bypass"
            )

    def test_xss_payload_not_reflected_in_error(self):
        """XSS payloads in request body must not appear in response."""
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": "<script>alert(1)</script>", "password": "x"},
            timeout=5,
        )
        assert "<script>" not in resp.text

    def test_oversized_payload_rejected(self):
        """Excessively large payloads must not cause 500 errors (DoS hardening)."""
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": "x" * 10_000, "password": "y" * 10_000},
            timeout=10,
        )
        assert resp.status_code in (400, 413, 429), (
            f"Oversized payload returned {resp.status_code} — should be rejected"
        )

    def test_session_cookie_httponly_flag(self):
        """HttpOnly prevents JavaScript from reading the session cookie."""
        resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        for cookie in resp.cookies:
            if "session" in cookie.name.lower():
                assert cookie.has_nonstandard_attr("HttpOnly") or cookie.get_nonstandard_attr("httponly") is not None or "httponly" in str(cookie).lower(), (
                    f"Session cookie '{cookie.name}' missing HttpOnly flag"
                )

    def test_login_does_not_enumerate_users(self):
        """
        The error message for wrong password vs unknown user must be identical.
        Different messages allow attackers to enumerate valid usernames.
        """
        wrong_user_resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": "nonexistent_xyz", "password": "wrong"},
            timeout=5,
        ).json()
        wrong_pass_resp = requests.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": "definitely_wrong"},
            timeout=5,
        ).json()
        assert wrong_user_resp.get("error") == wrong_pass_resp.get("error"), (
            "Different error messages for wrong user vs wrong password — enables user enumeration"
        )

    def test_path_traversal_in_asset_key(self, auth_session):
        """Path traversal in asset key must not return 500 or expose filesystem."""
        for payload in ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd"]:
            resp = auth_session.get(f"{WEB_APP_URL}/api/assets/{payload}", timeout=5)
            assert resp.status_code in (200, 400, 403, 404, 503), (
                f"Path traversal '{payload}' returned {resp.status_code}"
            )
            assert "root:" not in resp.text  # /etc/passwd content must never appear


# ---------------------------------------------------------------------------
# Performance baseline tests — p95 response time must be within SLA
# (01-memo.md sizing: web-app p95 < 800ms, EBA cloud guidelines)
# ---------------------------------------------------------------------------
class TestPerformance:

    def _measure_ms(self, fn, n=5) -> list[float]:
        import time
        times = []
        for _ in range(n):
            t0 = time.perf_counter()
            fn()
            times.append((time.perf_counter() - t0) * 1000)
        return sorted(times)

    def test_healthz_p95_under_200ms(self):
        """Health check is polled by ECS every 15s — must be fast."""
        times = self._measure_ms(lambda: requests.get(f"{WEB_APP_URL}/healthz", timeout=5))
        p95 = times[int(len(times) * 0.95) - 1] if len(times) >= 20 else max(times)
        assert p95 < 200, f"/healthz p95={p95:.0f}ms exceeds 200ms threshold"

    def test_login_p95_under_500ms(self):
        """Login involves DB query + bcrypt — allow up to 500ms."""
        def do_login():
            requests.post(
                f"{WEB_APP_URL}/login",
                json={"username": TEST_USER, "password": TEST_PASSWORD},
                timeout=5,
            )
        times = self._measure_ms(do_login)
        p95 = max(times)
        assert p95 < 500, f"/login p95={p95:.0f}ms exceeds 500ms threshold"

    def test_accounts_p95_under_800ms(self, auth_session):
        """Accounts endpoint SLA per 01-memo.md: p95 < 800ms (EBA cloud guidelines)."""
        times = self._measure_ms(
            lambda: auth_session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        )
        p95 = max(times)
        assert p95 < 800, f"/api/accounts p95={p95:.0f}ms exceeds 800ms SLA"


# ---------------------------------------------------------------------------
# End-to-end test — full user journey from login to data access to logout
# ---------------------------------------------------------------------------
class TestEndToEnd:

    def test_full_user_journey(self, db_conn, s3_client):
        """
        Simulates a Finance team member's complete session:
        1. Login → receive team claim
        2. Fetch accounts → verify schema
        3. Upload and access a static asset via presigned URL
        4. Logout → session invalidated
        5. Confirm protected endpoints return 401 after logout
        """
        session = requests.Session()

        # Step 1 — Login
        resp = session.post(
            f"{WEB_APP_URL}/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.status_code == 200
        assert resp.json()["team"] == "finance"

        # Step 2 — Seed and fetch accounts
        cur = db_conn.cursor()
        cur.execute(
            "INSERT INTO finance.accounts (account_name, team, balance, currency)"
            " VALUES ('E2E Test Account', 'finance', 9999.99, 'EUR')"
            " ON CONFLICT DO NOTHING RETURNING account_id"
        )
        db_conn.commit()
        cur.close()

        resp = session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code == 200
        accounts = resp.json()
        assert isinstance(accounts, list)
        names = [a["account_name"] for a in accounts]
        assert "E2E Test Account" in names

        # Step 3 — Upload asset and get presigned URL
        s3_client.put_object(Bucket="web-assets", Key="e2e/test-asset.pdf", Body=b"%PDF-1.4")
        resp = session.get(f"{WEB_APP_URL}/api/assets/e2e/test-asset.pdf", timeout=5)
        assert resp.status_code == 200
        assert "url" in resp.json()
        presigned_url = resp.json()["url"]
        assert "e2e/test-asset.pdf" in presigned_url or "test-asset" in presigned_url

        # Step 4 — Logout
        resp = session.post(f"{WEB_APP_URL}/logout", timeout=5)
        assert resp.status_code == 200

        # Step 5 — Verify session is dead
        resp = session.get(f"{WEB_APP_URL}/api/accounts", timeout=5)
        assert resp.status_code == 401, "Session still active after logout"

        # Cleanup
        s3_client.delete_object(Bucket="web-assets", Key="e2e/test-asset.pdf")
        session.close()

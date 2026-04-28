"""
Shared fixtures for all test suites.
Each fixture reads config from environment variables so the same suite runs
against docker-compose locally and against the real AWS stack in CI.
"""

import os

import boto3
import psycopg2
import pytest
import redis as redis_lib
import requests

# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------
WEB_APP_URL    = os.environ.get("WEB_APP_URL",    "http://localhost:5000")
DATABASE_URL   = os.environ.get("DATABASE_URL",   "postgresql://contoso_user:changeme_local@localhost:5432/contoso")
REDIS_URL      = os.environ.get("REDIS_URL",      "redis://localhost:6379")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9000")
MINIO_USER     = os.environ.get("MINIO_ROOT_USER",     "minioadmin")
MINIO_PASSWORD = os.environ.get("MINIO_ROOT_PASSWORD", "changeme_local")

# Test credentials seeded by V1__init_schema.sql
TEST_USER     = "finance.user"
TEST_PASSWORD = "changeme_local"


# ---------------------------------------------------------------------------
# Infrastructure fixtures (session-scoped — one connection for all tests)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def db_conn():
    conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def redis_client():
    client = redis_lib.from_url(REDIS_URL, socket_connect_timeout=5)
    yield client


@pytest.fixture(scope="session")
def s3_client():
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=MINIO_USER,
        aws_secret_access_key=MINIO_PASSWORD,
        region_name="eu-west-1",
    )


# ---------------------------------------------------------------------------
# Web-app auth fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="function")
def auth_session():
    """Authenticated requests.Session (finance.user). Logs out on teardown."""
    s = requests.Session()
    resp = s.post(
        f"{WEB_APP_URL}/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
        timeout=5,
    )
    assert resp.status_code == 200, f"Test login failed: {resp.status_code} {resp.text}"
    yield s
    s.post(f"{WEB_APP_URL}/logout", timeout=5)
    s.close()


@pytest.fixture(scope="function")
def anon_session():
    """Unauthenticated requests.Session."""
    s = requests.Session()
    yield s
    s.close()


@pytest.fixture(scope="function")
def auth_session_for_team():
    """
    Factory fixture: returns an authenticated session for any team.
    Usage: auth_session_for_team('risk') → session authenticated as risk.user
    """
    sessions = []

    def _make(team: str):
        s = requests.Session()
        resp = s.post(
            f"{WEB_APP_URL}/login",
            json={"username": f"{team}.user", "password": TEST_PASSWORD},
            timeout=5,
        )
        assert resp.status_code == 200, f"Login for {team}.user failed: {resp.text}"
        sessions.append(s)
        return s

    yield _make

    for s in sessions:
        s.post(f"{WEB_APP_URL}/logout", timeout=5)
        s.close()


# ---------------------------------------------------------------------------
# Batch test helpers
# ---------------------------------------------------------------------------
BATCH_BUCKET = os.environ.get("S3_BUCKET_OUTPUT", "reconciliation-output")


@pytest.fixture(scope="function")
def clean_s3_prefix(s3_client):
    """
    Yields a helper that deletes all objects under a given S3 prefix.
    Used by batch tests to ensure a clean state before each run.
    """
    cleaned: list[str] = []

    def _clean(prefix: str):
        cleaned.append(prefix)
        try:
            resp = s3_client.list_objects_v2(Bucket=BATCH_BUCKET, Prefix=prefix)
            for obj in resp.get("Contents", []):
                s3_client.delete_object(Bucket=BATCH_BUCKET, Key=obj["Key"])
        except Exception:
            pass

    yield _clean

    # Teardown: clean prefixes registered during the test
    for prefix in cleaned:
        try:
            resp = s3_client.list_objects_v2(Bucket=BATCH_BUCKET, Prefix=prefix)
            for obj in resp.get("Contents", []):
                s3_client.delete_object(Bucket=BATCH_BUCKET, Key=obj["Key"])
        except Exception:
            pass

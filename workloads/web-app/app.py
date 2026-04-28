import json
import logging
import sys
from functools import wraps

import boto3
import psycopg2
import psycopg2.pool
import redis as redis_lib
from botocore.exceptions import BotoCoreError, ClientError
from flask import Flask, g, jsonify, request, session
from flask_session import Session

from config import Config

# ---------------------------------------------------------------------------
# Structured JSON logger — secret values are never emitted
# ---------------------------------------------------------------------------
_SECRET_KEYS = {"password", "secret", "key", "token", "url", "dsn"}

def _log(level: str, event: str, **ctx):
    safe = {k: v for k, v in ctx.items() if not any(s in k.lower() for s in _SECRET_KEYS)}
    logging.getLogger(__name__).log(
        getattr(logging, level.upper()),
        json.dumps({"level": level, "event": event, **safe}),
    )

logging.basicConfig(stream=sys.stdout, level=Config.LOG_LEVEL, format="%(message)s")

# ---------------------------------------------------------------------------
# App bootstrap
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config.from_object(Config)

# Redis session store with graceful degradation to filesystem
_redis: redis_lib.Redis | None = None
try:
    _redis = redis_lib.from_url(Config.REDIS_URL, socket_connect_timeout=2)
    _redis.ping()
    app.config["SESSION_TYPE"] = "redis"
    app.config["SESSION_REDIS"] = _redis
    _log("info", "session_store_redis_ok")
except Exception:
    _log("warning", "session_store_redis_unavailable_using_filesystem")
    app.config["SESSION_TYPE"] = "filesystem"

Session(app)

# DB connection pool — minconn=1 keeps a warm connection without holding too many
_pool = psycopg2.pool.ThreadedConnectionPool(
    minconn=1,
    maxconn=10,
    dsn=Config.DATABASE_URL,
    connect_timeout=5,
)


def get_db() -> psycopg2.extensions.connection:
    if "db" not in g:
        g.db = _pool.getconn()
    return g.db


@app.teardown_appcontext
def release_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        _pool.putconn(db)


# S3 / MinIO client — endpoint_url is None in AWS (uses default), set for MinIO
_s3 = boto3.client(
    "s3",
    endpoint_url=Config.S3_ENDPOINT_URL,
    region_name=Config.AWS_REGION,
)

# ---------------------------------------------------------------------------
# Auth decorator
# ---------------------------------------------------------------------------
def require_login(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/healthz")
def healthz():
    """ECS health check — verifies DB and Redis connectivity."""
    checks: dict[str, str] = {}

    try:
        cur = get_db().cursor()
        cur.execute("SELECT 1")
        cur.close()
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = "error"
        _log("error", "healthz_db_failed", error=str(exc))

    if _redis is not None:
        try:
            _redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "degraded"  # app still serves; sessions fall back to filesystem
    else:
        checks["redis"] = "degraded"

    overall = "ok" if checks.get("db") == "ok" else "error"
    return jsonify({"status": overall, "checks": checks}), 200 if overall == "ok" else 503


@app.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    try:
        cur = get_db().cursor()
        # pgcrypto crypt() — password_hash stores the bcrypt hash
        cur.execute(
            "SELECT id, team FROM app.users"
            " WHERE username = %s AND password_hash = crypt(%s, password_hash)",
            (username, password),
        )
        row = cur.fetchone()
        cur.close()
    except Exception as exc:
        _log("error", "login_db_error", error=str(exc))
        return jsonify({"error": "service unavailable"}), 503

    if row is None:
        _log("warning", "login_failed", username=username)
        return jsonify({"error": "invalid credentials"}), 401

    session["user_id"] = row[0]
    session["team"] = row[1]
    _log("info", "login_success", user_id=row[0], team=row[1])
    return jsonify({"status": "ok", "team": row[1]}), 200


@app.post("/logout")
def logout():
    user_id = session.get("user_id")
    session.clear()
    _log("info", "logout", user_id=user_id)
    return jsonify({"status": "ok"}), 200


@app.get("/api/accounts")
@require_login
def accounts():
    team = session["team"]
    try:
        cur = get_db().cursor()
        cur.execute(
            "SELECT account_id, account_name, balance, currency"
            " FROM finance.accounts WHERE team = %s ORDER BY account_name",
            (team,),
        )
        rows = cur.fetchall()
        cur.close()
    except Exception as exc:
        _log("error", "accounts_db_error", error=str(exc), team=team)
        return jsonify({"error": "service unavailable"}), 503

    _log("info", "accounts_fetched", team=team, count=len(rows))
    return jsonify([
        {"account_id": r[0], "account_name": r[1], "balance": float(r[2]), "currency": r[3]}
        for r in rows
    ]), 200


@app.get("/api/assets/<path:key>")
@require_login
def asset_presign(key: str):
    """Returns a 5-minute pre-signed URL for a static asset in S3/MinIO."""
    try:
        url = _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": Config.S3_BUCKET_ASSETS, "Key": key},
            ExpiresIn=300,
        )
    except (BotoCoreError, ClientError) as exc:
        _log("error", "presign_failed", error=str(exc), asset_key=key)
        return jsonify({"error": "asset not available"}), 503

    return jsonify({"url": url}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=Config.DEBUG)

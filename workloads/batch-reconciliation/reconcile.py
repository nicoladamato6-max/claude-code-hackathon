"""
Nightly reconciliation batch job.

Idempotency contract: for a given job_date, running this script N times
produces the same S3 output. If a completed marker already exists in S3
for that date, the job exits 0 immediately without touching the database.
"""

import json
import os
import sys
from datetime import date, datetime

import boto3
import psycopg2
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Config — all values from environment, no filesystem reads
# ---------------------------------------------------------------------------
DATABASE_URL: str = os.environ["DATABASE_URL"]
S3_ENDPOINT_URL: str | None = os.environ.get("S3_ENDPOINT_URL")
S3_BUCKET_OUTPUT: str = os.environ.get("S3_BUCKET_OUTPUT", "reconciliation-output")
AWS_REGION: str = os.environ.get("AWS_REGION", "eu-west-1")
JOB_DATE: str = os.environ.get("JOB_DATE", date.today().isoformat())  # YYYY-MM-DD

# ---------------------------------------------------------------------------
# Structured JSON logger — never emits DATABASE_URL or credentials
# ---------------------------------------------------------------------------
def _log(level: str, event: str, **ctx) -> None:
    record = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "event": event,
        "job_date": JOB_DATE,
        **ctx,
    }
    print(json.dumps(record), flush=True)


# ---------------------------------------------------------------------------
# S3 helpers
# ---------------------------------------------------------------------------
_s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT_URL,
    region_name=AWS_REGION,
)

_DONE_KEY = f"{JOB_DATE}/completed.marker"
_FAIL_KEY = f"{JOB_DATE}/failed.marker"
_REPORT_KEY = f"{JOB_DATE}/reconciliation_report.json"


def _s3_key_exists(key: str) -> bool:
    try:
        _s3.head_object(Bucket=S3_BUCKET_OUTPUT, Key=key)
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "404":
            return False
        raise


def _s3_put_json(key: str, payload: dict) -> None:
    _s3.put_object(
        Bucket=S3_BUCKET_OUTPUT,
        Key=key,
        Body=json.dumps(payload, default=str),
        ContentType="application/json",
    )


def _s3_put_marker(key: str) -> None:
    _s3.put_object(Bucket=S3_BUCKET_OUTPUT, Key=key, Body=b"")


# ---------------------------------------------------------------------------
# Reconciliation logic
# ---------------------------------------------------------------------------
def reconcile(conn: psycopg2.extensions.connection) -> dict:
    cur = conn.cursor()

    cur.execute(
        """
        SELECT t.transaction_id, t.amount, t.currency, t.status,
               r.reconciled_amount, r.reconciled_at
        FROM finance.transactions t
        LEFT JOIN finance.reconciliation_log r
               ON r.transaction_id = t.transaction_id
              AND r.reconciliation_date = %s
        WHERE t.transaction_date = %s
        """,
        (JOB_DATE, JOB_DATE),
    )
    rows = cur.fetchall()
    cur.close()

    reconciled, failed = [], []
    for tx_id, amount, currency, status, rec_amount, rec_at in rows:
        if rec_at is not None:
            # Already reconciled in a previous run (idempotency)
            reconciled.append({"transaction_id": tx_id, "status": "already_reconciled"})
            continue

        if status == "settled" and amount is not None:
            reconciled.append({
                "transaction_id": tx_id,
                "amount": float(amount),
                "currency": currency,
            })
        else:
            failed.append({
                "transaction_id": tx_id,
                "reason": f"unreconcilable status={status}",
            })

    return {
        "job_date": JOB_DATE,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "records_processed": len(rows),
        "records_reconciled": len(reconciled),
        "records_failed": len(failed),
        "reconciled": reconciled,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    _log("info", "job_started")

    # Idempotency check — do not re-process a completed date
    if _s3_key_exists(_DONE_KEY):
        _log("info", "job_skipped_already_completed")
        return 0

    try:
        conn = psycopg2.connect(DATABASE_URL, connect_timeout=10)
    except psycopg2.OperationalError as exc:
        _log("error", "db_connect_failed", error=str(exc))
        _s3_put_marker(_FAIL_KEY)
        return 1

    try:
        report = reconcile(conn)
    except Exception as exc:
        _log("error", "reconcile_failed", error=str(exc))
        _s3_put_marker(_FAIL_KEY)
        conn.close()
        return 1
    finally:
        conn.close()

    _log(
        "info",
        "reconcile_complete",
        records_processed=report["records_processed"],
        records_reconciled=report["records_reconciled"],
        records_failed=report["records_failed"],
    )

    try:
        _s3_put_json(_REPORT_KEY, report)
    except Exception as exc:
        _log("error", "s3_write_failed", error=str(exc))
        return 1

    if report["records_failed"] > 0:
        _log(
            "error",
            "job_finished_with_failures",
            records_failed=report["records_failed"],
        )
        _s3_put_marker(_FAIL_KEY)
        return 1

    _s3_put_marker(_DONE_KEY)
    _log("info", "job_finished_ok", records_processed=report["records_processed"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

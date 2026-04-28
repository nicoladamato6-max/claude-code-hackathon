# Workload: Nightly Reconciliation Batch Job

## What this is

A Python script that runs every night at 02:00 to reconcile transaction records
between the core banking system and the reporting database. Currently triggered
by a cron job on a dedicated on-prem server. Target: AWS Batch job, scheduled
via EventBridge Scheduler.

## Migration target

| Component | On-prem | Cloud equivalent | Local sim |
|-----------|---------|-----------------|-----------|
| Scheduler | Linux cron | EventBridge Scheduler | `docker compose run` |
| Compute | bare-metal Python | AWS Batch (Fargate) | Docker container |
| Source DB | Postgres on-prem | RDS PostgreSQL | `postgres` service in compose |
| Output bucket | NFS mount `/data/reconciled/` | S3 (`reconciliation-output`) | MinIO |
| Notifications | SMTP relay | SNS → SES | Log output only (local) |

## Claude guidance for this workload

- **Idempotency is critical**: the job must produce the same output if re-run for the
  same date. Use the processing date as the S3 object key prefix (e.g. `2026-04-28/`).
- **Failure mode**: on error, write a `.failed` marker to S3/MinIO, then exit non-zero
  so AWS Batch marks the job as FAILED and triggers the alarm. Do not swallow exceptions.
- **Dockerfile**: same multi-stage pattern as web-app. Non-root user. No `ENTRYPOINT`
  override needed — AWS Batch passes the command.
- **Secrets**: `DATABASE_URL` from AWS Secrets Manager (or env var locally). Never print it.
- **Logging**: structured JSON logs to stdout. AWS Batch forwards stdout to CloudWatch.
  Include `job_date`, `records_processed`, `records_failed` in every log line.
- **No filesystem writes**: do not write to local disk. All output goes to S3/MinIO.

## Known issues discovered in Discovery (Challenge 2)

- Script reads `SOURCE_DB_HOST` from `/etc/batch/config` — a filesystem mount that won't
  exist in a container. Replace with env var.
- Output path is hardcoded as `/data/reconciled/` — replace with S3 client.
- No exit code handling: script always exits 0 even on partial failure.

## Timing and SLA

- Must complete by 04:00 (2-hour window).
- SRE on-call is paged if the job hasn't produced output by 04:15.
- Runbook: `docs/08-rollback-plan.md` section "batch-reconciliation".

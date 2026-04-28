# The Undo — Rollback Plan

**Status:** Draft  
**Owner:** SRE  
**Audience:** On-call engineers (readable at 4am)

## Principles

- Each step is reversible before the next step begins.
- Rollback is per-workload, per-stage — not "revert everything."
- Decision to roll back requires acknowledgement from on-call lead.

---

## Workload: web-app

### Stage 1 — Traffic cut (DNS switch to ALB)

**Trigger:** Error rate > 1% for 5 minutes on ALB access logs.

1. In Route 53, revert the A/ALIAS record for `app.contoso.com` to the on-prem IP.
2. TTL is 60s — wait 2 minutes for DNS propagation.
3. Verify on-prem is serving traffic: `curl -I https://app.contoso.com/healthz`
4. Keep the ECS service running (do not scale to 0) until root cause is identified.

### Stage 2 — Database cutover (RDS promoted to primary)

**Trigger:** Query error rate > 0.1% or replication lag > 30s.

1. Stop the ECS service: `aws ecs update-service --desired-count 0 ...`
2. Point the app back at the on-prem Postgres by updating `DATABASE_URL` in Parameter Store.
3. Re-enable on-prem Postgres writes (was set read-only during cutover).
4. Restart ECS service pointing at on-prem DB.

---

## Workload: batch-reconciliation

### Stage 1 — First cloud run

**Trigger:** Job exits non-zero OR no output in S3 by 04:15.

1. Check CloudWatch logs for the failed AWS Batch job execution.
2. If data integrity issue: re-run the on-prem cron job manually for the same date.
3. Mark the S3 date prefix with a `.failed` marker (if not already present).
4. Notify Finance team that reconciliation output is delayed.

### Stage 2 — Scheduler cutover (EventBridge enabled, on-prem cron disabled)

**Trigger:** Two consecutive failed runs.

1. Re-enable the on-prem cron job: `crontab -e` on `batch-host-01`.
2. Disable the EventBridge Scheduler rule: `aws scheduler update-schedule --state DISABLED ...`
3. Verify next on-prem run completes successfully before decommissioning EventBridge rule.

---

## Workload: reporting-db

### Stage 1 — Read traffic switched to RDS

**Trigger:** Query errors from any consumer team within 15 minutes of cutover.

1. Update the `REPORTING_DB_URL` parameter in SSM Parameter Store to point back to on-prem.
2. Restart connection pools in consumer apps (or wait for pool timeout, max 5 min).
3. Verify on-prem DB is accepting read connections: `psql $ON_PREM_URL -c "SELECT 1;"`

### Stage 2 — Write traffic switched to RDS (replication reversed)

**Trigger:** Data integrity check failure or replication lag > 60s.

1. Halt all writes to RDS: set app to maintenance mode.
2. Let replication catch up (monitor lag: `aws rds describe-db-instances ...`).
3. If lag does not clear in 10 minutes: failback to on-prem as primary.
4. Contact DBA lead before proceeding.

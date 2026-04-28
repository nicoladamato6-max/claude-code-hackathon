# Runbook — Contoso Financial Cloud Operations

**Status:** Approved  
**Owner:** SRE Lead  
**Audience:** On-call engineer (readable at 4am)  
**Date:** 2026-04-28  
**Related:** `08-rollback-plan.md` (cutover rollback procedures)

---

## Quick reference — alarm → action

| Alarm | Meaning | First action |
|-------|---------|-------------|
| `web-app-5xx-rate` | ≥5 HTTP 5xx in 5 min | [→ §Web-app 5xx spike](#web-app-5xx-spike) |
| `batch-no-output-by-0415` | No S3 output by 04:15 | [→ §Batch job missing](#batch-job-missing) |
| `batch-job-failed` | AWS Batch job status = FAILED | [→ §Batch job failed](#batch-job-failed) |
| `batch-duration-warning` | Job running > 90 min | [→ §Batch job slow](#batch-job-slow) |
| `rds-cpu-high` | RDS CPU > 80% for 10 min | [→ §RDS CPU high](#rds-cpu-high) |
| `rds-connections-high` | > 400 connections | [→ §RDS connections high](#rds-connections-high) |
| `rds-replica-lag` | Replica lag > 60s | [→ §RDS replica lag](#rds-replica-lag) |
| `rds-free-storage` | < 10 GB free | [→ §RDS storage low](#rds-storage-low) |

---

## Web-app 5xx spike

**Symptom:** `web-app-5xx-rate` alarm fires. Users reporting errors on login or account page.

```bash
# 1. Check ECS task health
aws ecs describe-services \
  --cluster contoso-cluster \
  --services web-app-service \
  --query 'services[0].{running:runningCount,desired:desiredCount,pending:pendingCount}'

# 2. Check recent application logs (last 100 lines)
aws logs tail /contoso/web-app --since 15m --follow

# 3. Check ALB target health
aws elbv2 describe-target-health \
  --target-group-arn $(aws elbv2 describe-target-groups \
    --names contoso-web-app-tg \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
```

**Decision tree:**

- All ECS tasks unhealthy → tasks are crash-looping:
  ```bash
  # Check stopped task exit reason
  aws ecs list-tasks --cluster contoso-cluster --desired-status STOPPED \
    | jq '.taskArns[0]' \
    | xargs aws ecs describe-tasks --cluster contoso-cluster --tasks \
    | jq '.tasks[0].containers[0].reason'
  ```
  Common causes: Secrets Manager unreachable (network ACL), DB connection refused (RDS SG).

- Tasks healthy but 5xx continuing → application-level error:
  ```bash
  aws logs filter-log-events \
    --log-group-name /contoso/web-app \
    --filter-pattern '"level":"error"' \
    --start-time $(date -d '15 minutes ago' +%s000)
  ```

- If RDS is unavailable → see [§RDS CPU high](#rds-cpu-high) or [§RDS failover](#rds-failover).

**Escalate if:** 5xx rate > 50% for > 5 minutes. Page SRE Lead.

---

## Batch job missing

**Symptom:** `batch-no-output-by-0415` alarm fires. No `YYYY-MM-DD/report.json` in S3 by 04:15 CET.

```bash
# 1. Check if job was even submitted by EventBridge (look at job queue)
TODAY=$(date +%Y-%m-%d)
aws batch list-jobs --job-queue contoso-batch-queue --job-status SUCCEEDED \
  --query "jobSummaryList[?createdAt > \`$(date -d 'yesterday 02:00' +%s000)\`]"

# 2. If no jobs found — EventBridge didn't trigger
aws scheduler list-schedules --group-name default \
  --query 'Schedules[?Name==`contoso-batch-nightly`].{State:State,Next:NextInvocationTime}'

# 3. Check S3 for .failed marker
aws s3 ls s3://contoso-reconciliation-output/$TODAY/
```

**Decision tree:**

- `.failed` marker present → job ran but failed. See [§Batch job failed](#batch-job-failed).
- `completed.marker` present → job already completed successfully (alarm is stale, acknowledge).
- No objects in S3 → job never ran. Check EventBridge schedule state; re-submit manually:
  ```bash
  aws batch submit-job \
    --job-name "manual-rerun-$TODAY" \
    --job-queue contoso-batch-queue \
    --job-definition contoso-batch-reconciliation \
    --parameters date=$TODAY
  ```

---

## Batch job failed

**Symptom:** `batch-job-failed` alarm fires or `.failed` marker present in S3.

```bash
# 1. Get failed job ID
FAILED_JOB=$(aws batch list-jobs \
  --job-queue contoso-batch-queue \
  --job-status FAILED \
  --query 'jobSummaryList[0].jobId' --output text)

# 2. Get exit reason
aws batch describe-jobs --jobs $FAILED_JOB \
  --query 'jobs[0].{status:status,reason:statusReason,exitCode:container.exitCode}'

# 3. Read container logs
aws logs get-log-events \
  --log-group-name /contoso/batch \
  --log-stream-name "$(aws batch describe-jobs --jobs $FAILED_JOB \
    --query 'jobs[0].container.logStreamName' --output text)"
```

**Fix and re-run:**
```bash
TODAY=$(date +%Y-%m-%d)

# Remove .failed marker to allow re-run (idempotency guard only blocks on completed.marker)
aws s3 rm s3://contoso-reconciliation-output/$TODAY/reconciliation.failed

# Re-submit
aws batch submit-job \
  --job-name "retry-$TODAY" \
  --job-queue contoso-batch-queue \
  --job-definition contoso-batch-reconciliation \
  --parameters date=$TODAY
```

**Escalate if:** job fails 2 consecutive runs. Page DBA — possible DB schema issue.

---

## Batch job slow

**Symptom:** `batch-duration-warning` alarm fires. Job running > 90 minutes.

```bash
# Check current job progress via structured logs
aws logs tail /contoso/batch --since 90m \
  | jq 'select(.records_processed != null) | {processed: .records_processed, failed: .records_failed}'
```

- If `records_processed` is incrementing → job is running slowly (large dataset). Monitor; the 2-hour hard timeout will terminate it cleanly with exit 1.
- If no log output for > 10 minutes → job hung. Force-terminate:
  ```bash
  RUNNING_JOB=$(aws batch list-jobs --job-queue contoso-batch-queue \
    --job-status RUNNING --query 'jobSummaryList[0].jobId' --output text)
  aws batch terminate-job --job-id $RUNNING_JOB --reason "manual-terminate-duration-exceeded"
  ```

---

## RDS CPU high

**Symptom:** `rds-cpu-high` alarm fires (CPU > 80% for 10 min).

```bash
# 1. Find top queries via Performance Insights
aws pi get-resource-metrics \
  --service-type RDS \
  --identifier db:$(aws rds describe-db-instances \
    --db-instance-identifier contoso-reporting \
    --query 'DBInstances[0].DbiResourceId' --output text) \
  --metric-queries '[{"Metric":"db.load.avg","GroupBy":{"Group":"db.sql","Limit":5}}]' \
  --start-time $(date -d '15 minutes ago' --iso-8601=seconds) \
  --end-time $(date --iso-8601=seconds) \
  --period-in-seconds 60

# 2. Check active connections in psql
psql $DATABASE_URL -c "
  SELECT pid, usename, application_name, state, query_start,
         left(query, 80) AS query
  FROM pg_stat_activity
  WHERE state != 'idle'
  ORDER BY query_start;"
```

**Common causes:**
- Missing index on finance/risk query → DBA adds index, no restart needed.
- `pg_cron` job overlapping with peak traffic → adjust cron schedule in V3 migration.
- Runaway reporting query → `SELECT pg_cancel_backend(pid)` for the offending PID.

---

## RDS connections high

**Symptom:** `rds-connections-high` alarm fires (> 400 connections for 5 min).

```bash
psql $DATABASE_URL -c "
  SELECT usename, application_name, count(*), state
  FROM pg_stat_activity
  GROUP BY usename, application_name, state
  ORDER BY count DESC;"
```

- ECS tasks leaking connections → check `app.py` — psycopg2 connection is opened per request without a pool. **Immediate fix:** restart ECS service to drain stale connections:
  ```bash
  aws ecs update-service \
    --cluster contoso-cluster \
    --service web-app-service \
    --force-new-deployment
  ```

**Long-term fix (Phase 2):** PgBouncer connection pooler in front of RDS.

---

## RDS replica lag

**Symptom:** `rds-replica-lag` alarm fires (replica lag > 60s).

```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/RDS \
  --metric-name ReplicaLag \
  --dimensions Name=DBInstanceIdentifier,Value=contoso-reporting-replica \
  --start-time $(date -d '30 minutes ago' --iso-8601=seconds) \
  --end-time $(date --iso-8601=seconds) \
  --period 60 --statistics Average
```

Replica lag is not production-critical (replica is read-only analytics). Alert DBA for awareness.  
**If lag > 300s:** pause analytics queries to replica temporarily.

---

## RDS storage low

**Symptom:** `rds-free-storage` alarm fires (< 10 GB free).

```bash
# Check current allocated vs used
aws rds describe-db-instances \
  --db-instance-identifier contoso-reporting \
  --query 'DBInstances[0].{AllocatedGB:AllocatedStorage,StorageType:StorageType}'
```

RDS `storage_autoscaling` is enabled (max 200 GB) — this alarm means autoscaling already triggered and is approaching the 200 GB ceiling. **Action:** increase `max_allocated_storage` in Terraform:
```bash
cd infra/reporting
# Edit variables.tf: max_allocated_storage → 500
terraform apply -target=aws_db_instance.reporting
```

---

## RDS failover (Multi-AZ automatic)

**Symptom:** RDS event: "Multi-AZ instance failover completed". Web-app may have a 30–60s blip.

```bash
# Verify new primary AZ
aws rds describe-db-instances \
  --db-instance-identifier contoso-reporting \
  --query 'DBInstances[0].{AvailabilityZone:AvailabilityZone,MultiAZ:MultiAZ,Status:DBInstanceStatus}'

# Verify ECS tasks reconnected (check for connection errors in logs after failover)
aws logs filter-log-events \
  --log-group-name /contoso/web-app \
  --filter-pattern '"level":"error"' \
  --start-time $(date -d '10 minutes ago' +%s000)
```

Failover is automatic. No action required if ECS tasks reconnect within 2 minutes.  
**If ECS tasks are stuck:** force new deployment (triggers new DB connections):
```bash
aws ecs update-service --cluster contoso-cluster --service web-app-service --force-new-deployment
```

---

## Secrets rotation

| Secret | Rotation | Procedure |
|--------|---------|---------|
| RDS master password | Automatic (30-day, RDS managed) | No action required |
| Flask SECRET_KEY | Manual every 90 days | Update Secrets Manager → ECS new deployment (active sessions invalidated) |
| Per-team DB passwords | Manual every 90 days | Update Secrets Manager → notify team leads → connection pool flush |

**Rotating Flask SECRET_KEY:**
```bash
# 1. Generate new key
NEW_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 2. Update Secrets Manager
aws secretsmanager put-secret-value \
  --secret-id contoso/web-app/secret-key \
  --secret-string "{\"SECRET_KEY\": \"$NEW_KEY\"}"

# 3. Force new ECS deployment (tasks pick up new secret on start)
aws ecs update-service --cluster contoso-cluster --service web-app-service --force-new-deployment

# Note: all active sessions are invalidated on SECRET_KEY rotation
# Notify users (Slack #product-announcements) 5 minutes before
```

---

## Useful CloudWatch log queries (Logs Insights)

```sql
-- Top errors in the last hour
fields @timestamp, level, message, error
| filter level = "error"
| stats count() as error_count by message
| sort error_count desc
| limit 20

-- Batch job summary for a specific date
fields @timestamp, records_processed, records_failed, duration_seconds
| filter ispresent(records_processed)
| filter job_date = "2026-04-28"

-- Slow API requests (> 1s)
fields @timestamp, method, path, status_code, duration_ms
| filter duration_ms > 1000
| sort duration_ms desc
| limit 50
```

---

## Contact escalation path

| Severity | Condition | First contact | Escalate to |
|----------|-----------|--------------|-------------|
| P1 — Critical | Web-app down > 5 min | SRE on-call (PagerDuty) | SRE Lead + CTO within 15 min |
| P2 — High | Batch failed 2 runs | SRE on-call | DBA + Finance Team Lead |
| P3 — Medium | RDS alarm sustained | SRE on-call | DBA |
| P4 — Low | Replica lag, slow queries | DBA (business hours) | — |

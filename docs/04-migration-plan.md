# Migration Plan — Contoso Financial

**Status:** Approved  
**Owner:** PM / Architect  
**Audience:** PM, SRE Lead, Finance Team Lead, DBA  
**Date:** 2026-04-28  
**Go-live target:** 10 weeks from start

---

## Principles

1. **No big-bang cutover** — each workload migrates independently with its own cutover window.
2. **Run-parallel before cutover** — new and old stacks run in parallel for at least 48 hours before DNS switchover.
3. **Rollback < 10 minutes at every stage** — see `08-rollback-plan.md` for per-stage procedure.
4. **Finance monthly close window is protected** — reporting-db cutover never overlaps the last 3 business days of any month (17:00–19:00 CET), per Finance Team Lead interview (`02-discovery.md §9`).
5. **Same image in all environments** — configuration via env vars only, no environment-specific rebuilds.

---

## Migration sequence

Complexity order (ADR §Migration sequence): web-app → batch → reporting-db.

```
Week 1–3    web-app           Containerised Flask app → ECS Fargate
Week 4–6    batch-recon.      Python batch job → AWS Batch + EventBridge
Week 7–10   reporting-db      PostgreSQL → RDS PG15 Multi-AZ
```

**Why this order:**
- web-app has no DB schema changes and is the fastest to validate (HTTP smoke tests).
- batch depends on the same DB as web-app but writes to S3 — isolated blast radius.
- reporting-db cutover is the highest risk (data migration + schema changes) and is last.

---

## Week-by-week plan

### Weeks 1–3 — web-app

| Day | Activity | Owner | Acceptance |
|-----|----------|-------|------------|
| W1D1 | `infra/shared` terraform apply (ECR, S3, SNS, CloudTrail, Budgets) | SRE | `terraform plan` shows 0 changes on re-run |
| W1D2–3 | Build web-app Docker image, push to ECR, smoke test locally | Dev | `docker run` + `/healthz` returns 200 |
| W1D4–5 | `infra/web-app` terraform apply (ECS, ALB, WAF, ACM) | SRE | ALB health check passes |
| W2D1–2 | Secrets Manager populated (SECRET_KEY, DB creds, Redis URL) | SRE+DBA | `aws secretsmanager get-secret-value` works |
| W2D3–5 | Parallel run: ECS + on-prem both serve traffic | SRE | CloudWatch shows 0 5xx on ECS |
| W3D1–2 | DNS switchover (low-traffic window, Tuesday 02:00 CET) | SRE | p95 latency within SLA (<500ms /login) |
| W3D3–5 | Monitor for 48 h; decommission on-prem web server | SRE | Zero rollback events |

**Go/no-go criteria before DNS switchover:**
- [ ] ECS tasks stable (no restarts) for 24 h
- [ ] `/healthz` p95 < 200 ms
- [ ] WAF blocking test SQLi payload (manual check)
- [ ] pgaudit generating log entries in CloudWatch

---

### Weeks 4–6 — batch-reconciliation

| Day | Activity | Owner | Acceptance |
|-----|----------|-------|------------|
| W4D1–2 | `infra/batch` terraform apply (Batch, EventBridge, alarms) | SRE | Job queue shows ENABLED |
| W4D3–4 | Manual test run: `aws batch submit-job` with yesterday's date | Dev | Exit code 0, S3 output `YYYY-MM-DD/report.json` present |
| W4D5 | Idempotency test: re-run same date | Dev | Second run skips, no duplicate output |
| W5D1–2 | Parallel run: AWS Batch + on-prem cron both run nightly | SRE+DBA | Row counts match within 0.01% |
| W5D3–5 | Cutover: disable on-prem cron, EventBridge scheduler becomes sole trigger | SRE | CloudWatch `no-output-by-04:15` alarm stays GREEN |
| W6D1–5 | Monitor alarms (job-failed, duration-warning) for 5 nights | SRE | Zero alarm fires |

**Go/no-go criteria before disabling on-prem cron:**
- [ ] 3 consecutive successful AWS Batch runs
- [ ] Exit codes propagate correctly (tested with injected failure)
- [ ] S3 `.failed` marker test: CloudWatch alarm fires within 15 min

---

### Weeks 7–10 — reporting-db

| Day | Activity | Owner | Acceptance |
|-----|----------|-------|------------|
| W7D1–2 | `infra/reporting` terraform apply (RDS, ElastiCache, AWS Backup) | SRE+DBA | RDS shows `available`, Multi-AZ secondary confirmed |
| W7D3–5 | Flyway migrate V1→V3 on empty RDS, smoke test schema | DBA | `pytest tests/smoke/ -k db` all green |
| W8D1–3 | pg_dump on-prem → pg_restore on RDS (with SCT for schema diffs) | DBA | Row counts match, MD5 checksums pass |
| W8D4–5 | Data integrity suite against RDS | DBA | `pytest tests/data-integrity/ -v` all 28 green |
| W9D1–2 | Per-team roles validated (finance_ro, risk_ro, etc.) | DBA+Sec | Each role can only SELECT own schema |
| W9D3–4 | Parallel run: web-app reads from RDS replica, writes go to on-prem | SRE | Zero read errors |
| W9D5 | **Cutover window: Wednesday W9, NOT last 3 business days of month** | SRE | |
| W10D1–5 | Monitor RDS alarms (CPU, replica lag, storage), decommission on-prem DB | SRE+DBA | All alarms GREEN for 5 days |

**Go/no-go criteria before reporting-db cutover:**
- [ ] Data integrity suite 28/28 green
- [ ] pgaudit logging verified in CloudWatch
- [ ] PITR tested: restore to point-in-time succeeds in < 30 min
- [ ] ElastiCache AOF confirmed: Redis restart test preserves session data
- [ ] Finance Team Lead sign-off (not within last 3 business days of month)

---

## Cutover day checklist (per workload)

```
□ Notify stakeholders (Slack #cloud-migration) 1 hour before
□ Take on-prem snapshot / pg_dump
□ Confirm CloudWatch alarms are GREEN baseline
□ Execute cutover (DNS / cron disable / DB endpoint swap)
□ Run smoke suite: pytest tests/smoke/ -v
□ Monitor for 15 minutes — no 5xx / alarm fires
□ Post go/no-go to #cloud-migration
□ If no-go: execute rollback procedure from 08-rollback-plan.md
□ Keep on-prem standby for 48 h before decommission
```

---

## Communication plan

| Milestone | Who | Channel | When |
|-----------|-----|---------|------|
| Phase start | All stakeholders | Email + Confluence | D-5 |
| Each cutover | SRE + impacted team leads | Slack #cloud-migration | D-1 + D-day |
| Go-live confirmation | CFO, CTO, Compliance | Email | Same day |
| Decommission confirmation | SRE Lead | ITSM ticket | +48 h |

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| Finance close window overlap | Low | High | Calendar check mandatory before any reporting-db cutover |
| pg_dump schema incompatibility (PG12→PG15) | Medium | High | SCT + rehearsal on staging before production |
| ElastiCache cold cache causes login spike | Low | Medium | Cache-aside pattern; session fallback to filesystem |
| AWS Batch job queue saturation | Low | Low | Queue CloudWatch alarm + 2-hour job timeout |
| RDS PITR window gap during migration | Low | High | pg_dump taken < 1 h before cutover; PITR enabled from day 1 |

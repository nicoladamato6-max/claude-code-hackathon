# Workload: Reporting Database

## What this is

A PostgreSQL 13 database used by five internal teams (Finance, Risk, Compliance,
Operations, Executive Reporting). Contains materialized views, scheduled refresh
jobs, and read-only replicas. Target: RDS PostgreSQL 15 with Multi-AZ.

## Migration target

| Component | On-prem | Cloud equivalent | Local sim |
|-----------|---------|-----------------|-----------|
| Primary DB | Postgres 13 bare-metal | RDS PostgreSQL 15 Multi-AZ | `postgres` service in compose |
| Read replica | manual streaming rep | RDS Read Replica | Not simulated locally |
| Backups | pg_dump to NFS | RDS automated backups + S3 | MinIO (`db-backups` bucket) |
| Schema migrations | manual scripts | Flyway / Liquibase in CI | Flyway container |

## Claude guidance for this workload

- **Read this first**: this database has undocumented dependencies. Do not rename
  columns, drop views, or change data types without checking `docs/02-discovery.md`
  for the full dependency map.
- **Postgres 15 compatibility**: several functions changed between PG13 and PG15.
  Run `pg_upgrade --check` before any schema migration. Known break: `to_timestamp`
  behavior with timezone differs — audit all callers.
- **Materialized views**: refresh schedule is currently driven by cron on the DB server.
  On RDS this must be replaced with an EventBridge rule calling a Lambda or pg_cron extension.
- **Data residency**: Compliance requires data to remain in `eu-west-1`. The RDS instance
  and all replicas must be in `eu-west-1`. Do not create cross-region replicas without
  explicit sign-off from Compliance.
- **Secrets**: `POSTGRES_PASSWORD` from AWS Secrets Manager. Rotation is enabled —
  the app must handle `OperationalError` on connection and retry once after re-fetching
  the secret.
- **Schema migrations**: always write reversible migrations (UP + DOWN). Never use
  `DROP TABLE` or `DROP COLUMN` directly — use soft-delete pattern (rename + deprecate).

## Known issues discovered in Discovery (Challenge 2)

- Three materialized views have `REFRESH MATERIALIZED VIEW` called via cron on the
  DB host at 01:00 — these will break on RDS unless pg_cron is enabled.
- Five consumer teams connect with the same `reporting_user` credentials — no row-level
  security in place. Document this as a post-migration hardening item.
- One view (`v_executive_summary`) joins a table from the core banking DB via `dblink`
  — this cross-DB link will need to be replaced with a data pipeline or federated query.

## Consumer teams and contacts

| Team | Read schema | Critical report | Acceptable downtime |
|------|-------------|-----------------|---------------------|
| Finance | `finance.*` | Monthly close (last biz day) | < 15 min |
| Risk | `risk.*` | Daily VaR (06:00) | < 5 min |
| Compliance | `compliance.*` | Regulatory filing (quarterly) | < 30 min |
| Operations | `ops.*` | Real-time dashboard | < 2 min |
| Executive | `exec.*` | Board pack (monthly) | < 1 hour |

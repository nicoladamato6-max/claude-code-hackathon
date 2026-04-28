# Contoso Financial — Cloud Migration

![CI](https://github.com/nicoladamato6-max/claude-code-hackathon/actions/workflows/ci.yml/badge.svg)

Three on-premise workloads migrated to AWS using a replatform strategy:
ECS Fargate + RDS PostgreSQL 15 + ElastiCache Redis + AWS Batch.

---

## The journey

### Phase 1 — Strategic alignment (docs/)

The engagement started with a genuine conflict: the CFO had signed a fixed-deadline
cloud contract while the CTO wanted cloud-native architecture. Before writing a line
of code, we produced documents that resolved the tension with evidence, not opinion.

**`docs/01-memo.md` — The decision**
Chose **replatform** (ECS + RDS, not Lambda + DynamoDB) because:
- A full refactor would take 3–6 months per workload; the contract deadline is fixed
- The AWS contract already permits PaaS services — Phase 2 cloud-native is contractually
  supported without renegotiation (CTO's concern addressed)
- 3-year TCO saving: **€723,400** vs on-prem. Breakeven: **7 months**.
  Primary driver: eliminating 2 sysadmin FTEs (€160k/year) via managed services

**`docs/02-discovery.md` — The findings**
Before touching the codebase we ran **stakeholder role-play interviews** with:
SRE Lead, DBA, Security Officer, Finance Team Lead, Developer.

The SRE "what keeps you up at night?" question alone surfaced 12 operational problems
invisible to static analysis — including a Redis crash that logs out every user, a
`pg_hba.conf` open to `0.0.0.0/0` for two years, and a batch job that always exits 0
even on partial failure.

Total: **36 findings** (7 Critical, 16 High, 9 Medium, 4 Low).
All 7 Critical and all 16 High findings are resolved in this engagement.

**`docs/03-options-adr.md` — The options**
Evaluated 4 architecture patterns with weighted scoring across 6 criteria.
Option B (Replatform) scored **64/75**, 15 points ahead of the next viable option.
**11 technology-level ADRs** document every AWS service choice (ECS vs EKS,
RDS vs Aurora, AWS Batch vs Lambda, ALB vs NLB, etc.) with alternatives
considered and "revisit if" conditions.

**`docs/04-migration-plan.md` — The plan**
Week-by-week execution plan with go/no-go criteria for each workload cutover,
Finance monthly close window enforcement, parallel-run periods, and a cutover
day checklist readable under pressure.

**`docs/05-security-review.md` — The threat model**
STRIDE threat model across all three workloads, OWASP Top 10 mapping,
network defence-in-depth diagram, secrets rotation schedule, and recommended
pre-go-live penetration test scope.

**`docs/06-compliance-checklist.md` — The compliance matrix**
GDPR art.32, EBA Cloud Outsourcing GL/2019/02, and EU AI Act Annex III —
**36/36 requirements met**, each mapped to the specific code or IaC control
that satisfies it, plus the automated tests that verify it.

**`docs/07-runbook.md` — The runbook**
On-call playbook with alarm→action table, step-by-step CLI procedures for every
CloudWatch alarm, secrets rotation procedures, and CloudWatch Logs Insights queries.
Designed to be readable and actionable at 4am.

---

### Phase 2 — Implementation (workloads/ + infra/)

Migration sequence follows complexity order per ADR §Migration sequence:

```
Week 1–3   web-app          Containerised Flask app → ECS Fargate
Week 4–6   batch-recon.     Python batch job → AWS Batch + EventBridge
Week 7–10  reporting-db     PostgreSQL → RDS PG15 Multi-AZ
```

Reporting-db cutover avoids the last 3 business days of each month
(Finance monthly close window, surfaced in the Finance Team Lead interview).

**Key implementation decisions carried from discovery into code:**

| Finding (discovery) | Code fix |
|--------------------|----|
| `DEBUG=True` in production | `Config.DEBUG` defaults `False`; reads `FLASK_DEBUG` env var |
| Redis crash = all users logged out | `ElastiCache` with AOF + replication; Flask falls back to filesystem sessions |
| Batch always exits 0 | `reconcile.py` propagates exceptions; exits 1 on any `records_failed > 0` |
| Batch silent retry masks failures | Removed; AWS Batch retry=1 + fail-fast |
| `SECURITY DEFINER` stored procs | Rewritten as `SECURITY INVOKER` in `V2__stored_procedures.sql` |
| `dblink` credential in plain view | `exec.v_executive_summary` rewritten as local join; dblink removed |
| Shared `reporting_user` password | 5 per-team read-only roles in `V3__roles_and_pgcron.sql` |
| Log retention 7 days on disk | CloudWatch Logs 90 days + S3 Glacier 1 year |
| OS patching requires downtime | ECS: patching = new image; zero SSH access to servers |

---

### Phase 3 — Testing (tests/)

Testing pyramid with four layers, **112 tests total**:

| Suite | Tests | What it verifies |
|-------|-------|-----------------|
| `tests/smoke/` | 22 | Services reachable, PG15 version confirmed, pgaudit active, no SECURITY DEFINER |
| `tests/contract/` | 45 | API contract, SQLi/XSS/path traversal, cookie HttpOnly, p95 SLAs, E2E journey |
| `tests/batch/` | 17 | Reconcile logic, structured logging, idempotency, S3 output format, exit codes |
| `tests/data-integrity/` | 28 | Row counts, MD5 checksums, business rules, pg_cron jobs, per-team roles |

Security tests verify: SQL injection in username/password, XSS reflection,
path traversal on asset keys, user enumeration prevention, cookie flags.

Performance baselines (from `01-memo.md` sizing): `/healthz` p95 < 200ms,
`/login` p95 < 500ms, `/api/accounts` p95 < 800ms (EBA cloud guidelines).

The CI pipeline (`.github/workflows/ci.yml`) runs all four layers with fail-fast
ordering, plus `terraform validate`, `ruff`/`mypy` lint, Trivy CVE scan, and
Checkov IaC security analysis on every push.

---

## Running locally

### Prerequisites

- Docker Desktop with Compose v2
- Python 3.12+
- `pip install -r tests/requirements.txt`

### Start the stack

```bash
docker compose up -d          # starts postgres, redis, minio, web-app
docker compose ps             # all services must show "healthy"
```

### Run tests

```bash
# Layer 1 — smoke (run first; if this fails, stop here)
pytest tests/smoke/ -v

# Layer 2 — contract + security + performance
pytest tests/contract/ -v

# Layer 3 — batch job
pytest tests/batch/ -v

# Layer 4 — data integrity (set SOURCE_DB_URL for cross-DB checks)
pytest tests/data-integrity/ -v

# Full suite
pytest tests/ -v --tb=short
```

### Run the batch job locally

```bash
docker compose run --rm \
  -e DATABASE_URL="postgresql://contoso_user:changeme_local@postgres:5432/contoso" \
  -e S3_ENDPOINT_URL="http://minio:9000" \
  -e S3_BUCKET_OUTPUT="reconciliation-output" \
  -e AWS_ACCESS_KEY_ID="minioadmin" \
  -e AWS_SECRET_ACCESS_KEY="changeme_local" \
  -e JOB_DATE="$(date +%Y-%m-%d)" \
  batch-reconciliation python reconcile.py
```

### Teardown

```bash
docker compose down -v        # stops containers and drops volumes
```

---

## Deploying to AWS

### Order of operations

```bash
# 1. Bootstrap shared resources (ECR, S3, SNS, CloudTrail, Budget)
cd infra/shared
terraform init && terraform apply

# 2. Build and push images to ECR
docker build -t <ecr_web_app_url>:v1.0.0 workloads/web-app/
docker build -t <ecr_batch_url>:v1.0.0   workloads/batch-reconciliation/
docker push <ecr_web_app_url>:v1.0.0
docker push <ecr_batch_url>:v1.0.0

# 3. Apply reporting (RDS + ElastiCache must exist before ECS connects)
cd infra/reporting
terraform init && terraform apply

# 4. Apply batch
cd infra/batch
terraform apply

# 5. Apply web-app last (references RDS + Redis endpoints)
cd infra/web-app
terraform apply
```

### Run DB migrations

```bash
# Flyway applies V1, V2, V3 in order
docker run --rm \
  -e FLYWAY_URL="jdbc:postgresql://<rds_endpoint>:5432/contoso" \
  -e FLYWAY_USER="contoso_admin" \
  -e FLYWAY_PASSWORD="<from Secrets Manager>" \
  -v $(pwd)/workloads/reporting-db/migrations:/flyway/sql \
  flyway/flyway:10 migrate
```

---

## Repo structure

```
contoso-financial/
├── docs/
│   ├── 01-memo.md              Strategic decision + TCO analysis
│   ├── 02-discovery.md         36 findings from stakeholder interviews
│   ├── 03-options-adr.md       Scored options + 11 technology ADRs
│   ├── 04-migration-plan.md    Week-by-week plan + go/no-go criteria
│   ├── 05-security-review.md   STRIDE threat model + OWASP Top 10
│   ├── 06-compliance-checklist.md  GDPR / EBA / EU AI Act — 36/36 ✅
│   ├── 07-runbook.md           On-call playbook (alarm → action)
│   └── 08-rollback-plan.md     Per-workload, per-stage rollback (readable at 4am)
│
├── workloads/
│   ├── web-app/                Flask app (app.py, config.py, Dockerfile)
│   ├── batch-reconciliation/   Nightly reconciler (reconcile.py, Dockerfile)
│   └── reporting-db/
│       └── migrations/         V1 schema, V2 stored procs, V3 roles + pg_cron
│
├── infra/
│   ├── shared/                 ECR, S3, SNS, CloudTrail, Budgets (apply first)
│   ├── web-app/                ECS Fargate + ALB + WAF WebACL
│   ├── batch/                  AWS Batch + EventBridge Scheduler + alarms
│   └── reporting/              RDS PG15 Multi-AZ + ElastiCache + AWS Backup
│
├── tests/
│   ├── conftest.py             Shared fixtures (db, redis, s3, auth sessions)
│   ├── smoke/                  22 tests — connectivity and version checks
│   ├── contract/               45 tests — API, security, performance, E2E
│   ├── batch/                  17 tests — reconcile logic and idempotency
│   └── data-integrity/         28 tests — checksums and business rules
│
├── .github/
│   └── workflows/ci.yml        CI: test pyramid + terraform validate + Trivy + Checkov
├── docker-compose.yml          Local cloud simulator (MinIO, Postgres, Redis, web-app)
├── pytest.ini
├── .gitignore                  *.tfstate, .env, .venv excluded
├── CLAUDE.md                   Conventions and patterns for this engagement
└── README.md                   This file
```

---

## Compliance summary

| Requirement | Implementation | Verified by |
|------------|---------------|-------------|
| GDPR data residency | All resources in `eu-west-1`; no cross-region replication | `infra/` provider blocks |
| GDPR encryption at rest | RDS AES-256, ElastiCache at-rest encryption, S3 SSE-AES256 | `tests/smoke/` |
| GDPR encryption in transit | TLS 1.3 on ALB, `transit_encryption_enabled` on ElastiCache | `infra/web-app/` + `infra/reporting/` |
| GDPR art.32 audit logging | `pgaudit` on RDS (query-level) + CloudTrail (API-level) | `tests/smoke/test_pgaudit_extension_installed` |
| GDPR access control | 5 per-team read-only roles; Secrets Manager audit via CloudTrail | `tests/data-integrity/` |
| EU AI Act (Annex III) | `ai-act-scope:high-risk` tag on RDS tables with customer identifiers | `V1__init_schema.sql` |
| EBA cloud outsourcing GL | Exit strategy in `docs/08-rollback-plan.md`; data portability via RDS snapshots | `docs/06-compliance-checklist.md` |

Full compliance matrix: `docs/06-compliance-checklist.md` — 36/36 requirements met.

---

## Phase 2 roadmap

| Initiative | Target | Benefit |
|-----------|--------|---------|
| Web-app → API Gateway + Lambda | Q4 2026 | Scale-to-zero; eliminate always-on ECS cost |
| Batch → Lambda + Step Functions | Q4 2026 | Parallel reconciliation; < 15 min completion |
| Reporting-db → Aurora Serverless v2 | Q1 2027 | Auto-scaling storage; pause during off-hours |
| Row-level security per team | Q3 2026 | Replace shared credentials with per-team isolation |
| AI/ML credit scoring pipeline | Q2 2027 | AI Act compliant; data stays in `eu-west-1` |

# Contoso Financial — Cloud Migration Engagement

## Project context

Contoso Financial is migrating three on-premise workloads to cloud. This repo contains
infrastructure-as-code, application source, and validation suites for the migration.

Local Docker Compose simulates the target cloud services:
- MinIO → AWS S3
- Postgres → AWS RDS (PostgreSQL)
- Redis → AWS ElastiCache

## Workloads

| Folder | Description |
|--------|-------------|
| `workloads/web-app/` | Customer-facing web application |
| `workloads/batch-reconciliation/` | Nightly reconciliation batch job |
| `workloads/reporting-db/` | Shared reporting database (5 consumer teams) |

Each workload has its own `CLAUDE.md` with workload-specific guidance. Read it before
making changes to that workload.

## Key constraints

- **No secrets in plaintext** — use environment variables or a secrets manager reference.
  A PreToolUse hook in `.claude/settings.json` blocks writes containing raw credentials.
- **IaC must be idempotent** — running `terraform apply` (or equivalent) twice must produce
  no changes on the second run.
- **State files must not be committed** — add `*.tfstate`, `*.tfstate.backup` to `.gitignore`.
- **Same container image in all environments** — swap config via env vars, never rebuild.

## Architectural decision

This engagement follows a **lift-and-shift first, then optimize** strategy (see `docs/01-memo.md`).
Refactoring happens post-migration, not during.

## Running locally

```bash
docker compose up -d          # start cloud simulators
docker compose ps             # verify all services healthy
docker compose down -v        # teardown (drops volumes)
```

## Testing

```bash
# Smoke tests — is the service reachable?
pytest tests/smoke/

# Contract tests — does the API behave as expected?
pytest tests/contract/

# Batch job tests — idempotency, exit codes, S3 output format
pytest tests/batch/

# Data integrity checks — did data survive the migration intact?
pytest tests/data-integrity/

# Full suite
pytest tests/ -v
```

---

## Conventions learned during this engagement

These are patterns Claude established while working on this project.
They encode decisions made during the hackathon and must be preserved.

### Discovery: stakeholder role-play interviews

Before writing any code, Claude conducted **simulated stakeholder interviews** with:
SRE Lead, DBA, Security Officer, Finance Team Lead, web-app Developer.
The SRE "what keeps you up at night?" question surfaced 12 operational problems
not visible from static code analysis. Always run this technique before defining
requirements — it surfaces non-obvious constraints (e.g. Finance monthly close
window, Redis crash = all users logged out, crontab credentials in plaintext).

Interview findings are in `docs/02-discovery.md`. Do not add new workload features
without first checking whether they introduce new findings.

### Architecture decisions: ADR with hard constraints first

All architecture decisions follow the pattern in `docs/03-options-adr.md`:
1. State **hard constraints** that eliminate options before scoring
2. Score remaining options with **weighted criteria** (weights reflect stakeholder priorities)
3. Map selected option to **SRE night-watch concerns** and **discovery findings**
4. Document **technology selection rationale** for every AWS service chosen
5. Include **"revisit if"** conditions so future engineers know when to re-evaluate

Do not add AWS services without a corresponding ADR entry explaining the choice
and what alternatives were considered and rejected.

### Terraform: one module per workload + shared module

```
infra/shared/    ← apply first: ECR, S3 buckets, SNS, CloudTrail, Budget
infra/web-app/   ← ECS Fargate + ALB + WAF
infra/batch/     ← AWS Batch + EventBridge + alarms
infra/reporting/ ← RDS + ElastiCache + AWS Backup
```

Every module must:
- Use `default_tags` in the AWS provider block for cost attribution
- Reference `infra/shared` outputs (SNS ARN, bucket names, ECR URLs)
- Use S3+DynamoDB remote state backend
- Output endpoints consumed by other modules

### Test pyramid convention

Tests are layered in execution order. A failing lower layer means the upper
layers should not be run (services are not up).

```
smoke/          → connectivity, versions, schema existence
contract/       → API behaviour, security headers, auth flows
batch/          → reconciliation logic, idempotency, S3 output format
data-integrity/ → row counts, checksums, business rules post-migration
```

Each layer has dedicated classes (e.g. `TestSecurity`, `TestPerformance`,
`TestIdempotency`). New tests go into the correct class — do not add security
tests to the smoke suite or performance tests to contract.

The CI pipeline (`.github/workflows/ci.yml`) enforces this ordering via
`fail-fast: true` on the matrix. Smoke must pass before contract runs.

### Secrets: never in code, always filtered from logs

The `_log()` helper in both `app.py` and `reconcile.py` filters any context
key containing `password`, `secret`, `key`, `token`, or `url`. This pattern
must be preserved when adding new log calls. The PreToolUse hook in
`.claude/settings.json` blocks writes containing raw credentials.

### Migration sequence constraint

Workloads must be migrated in this order: web-app → batch → reporting-db.
Reporting-db cutover must not overlap the last 3 business days of any month
(Finance monthly close window 17:00–19:00 CET). This constraint comes from
the Finance Team Lead interview (docs/02-discovery.md §9) and is encoded
in docs/03-options-adr.md §Migration sequence and docs/04-migration-plan.md.

### Documentation completeness

Every engagement must produce all 8 documents before go-live:

| Doc | Purpose |
|-----|---------|
| `01-memo.md` | Strategic decision + TCO |
| `02-discovery.md` | Stakeholder interview findings |
| `03-options-adr.md` | Architecture decision record |
| `04-migration-plan.md` | Week-by-week plan + go/no-go |
| `05-security-review.md` | STRIDE + OWASP mapping |
| `06-compliance-checklist.md` | GDPR / EBA / EU AI Act matrix |
| `07-runbook.md` | On-call operational playbook |
| `08-rollback-plan.md` | Per-stage rollback procedures |

Do not ship without all 8. Missing docs are a compliance gap (EBA GL/2019/02 requires
documented exit strategy, DR plan, and audit procedures before go-live).

### CI pipeline conventions

The GitHub Actions workflow (`.github/workflows/ci.yml`) has four jobs:

- **test** — matrix over the 4 test layers with `fail-fast: true`; uses
  `docker compose up -d --wait` to start the stack; Flyway migrations applied before tests.
- **lint** — `ruff check` + `mypy` on all workload and test Python files.
- **terraform-validate** — `terraform init -backend=false` + `validate` + `fmt -check`
  per module; no AWS credentials required.
- **security-scan** — Trivy filesystem scan (CRITICAL/HIGH exit-1) + Checkov IaC scan
  (advisory, soft_fail=true, SARIF uploaded to GitHub Security tab).

All new code must pass these checks before merge. Do not use `--no-verify` to skip hooks.

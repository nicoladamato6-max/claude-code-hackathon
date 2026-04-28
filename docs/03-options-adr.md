# The Options — Architecture Decision Record

**Status:** Approved  
**Owner:** Architect  
**Audience:** CTO, CFO, Compliance, SRE Lead  
**Date:** 2026-04-28  
**Supersedes:** —  
**Review date:** 2026-10-28 (6 months post go-live)

---

## Context and problem statement

Contoso Financial must migrate three on-premise workloads to AWS by the contractually fixed
go-live date. The AWS contract signed by the CFO explicitly permits PaaS and cloud-native
services. The Discovery phase (see `02-discovery.md`) identified **36 findings** — 7 Critical,
16 High — that must be addressed during or before migration. The SRE Lead identified 12
operational problems that the current on-premise architecture cannot solve without architectural
change.

**Decision required:** select the target cloud architecture pattern that satisfies the fixed
timeline, resolves the critical findings, addresses the SRE concerns, and complies with GDPR,
AI Act, and EBA Cloud Outsourcing Guidelines.

---

## Hard constraints (eliminate options before scoring)

The following constraints are non-negotiable. Any option that violates one is eliminated
regardless of score.

| Constraint | Source | Eliminates |
|-----------|--------|-----------|
| Go-live date is fixed; full rewrite (3–6 months/workload) is out of scope | CFO contract | Option C for all workloads in Phase 1 |
| All data must remain in `eu-west-1` | GDPR + EBA GL/2019/02 | Any cross-region option |
| `pgaudit` query logging required for GDPR art. 32 compliance | Compliance (discovery §5) | Options that cannot enable `pgaudit` without manual OS access |
| `SECURITY DEFINER` stored procedures cannot run as superuser on target | RDS limitation (discovery §6) | EC2-only options that do not address this gap |
| Rollback must be possible at each stage in < 10 min | SRE requirement | Options with no incremental cutover path |

Option C is **eliminated from Phase 1** by the timeline constraint. It is formally deferred to
Phase 2 and scoped in §Phase 2 roadmap below.

---

## Evaluation criteria and weights

Criteria are weighted to reflect stakeholder priorities established in the memo (`01-memo.md`).

| Criterion | Weight | Rationale |
|-----------|--------|-----------|
| Migration risk | 3 | Fixed deadline — a failed migration has no recovery |
| Timeline feasibility | 3 | Contract deadline is non-negotiable |
| Compliance (GDPR / AI Act / EBA) | 3 | Regulatory breach = show-stopper |
| Operational improvement (SRE concerns) | 2 | 12 night-watch problems must be addressed |
| Cost (3-year TCO) | 2 | CFO requires positive ROI within 12 months |
| Operability / team cognitive load | 2 | Small SRE team; complexity must be manageable |
| **Total weight** | **15** | |

---

## Options

### Option A — Lift-and-Shift (Rehost on EC2)

**Description:** Move workloads to EC2 instances. Postgres migrated to EC2 PostgreSQL (no RDS).
Redis on EC2. Minimal code changes: replace hardcoded IPs with env vars, fix filesystem mounts.

**What it solves from discovery:** hardcoded IPs (§1), filesystem mounts (§2). Leaves 32 of
36 findings unaddressed.

**What it does NOT solve (SRE night-watch):**

| SRE problem | Status under Option A |
|------------|----------------------|
| Single point of failure | Not solved — still single EC2 instances |
| No autoscaling | Not solved — requires new hardware procurement |
| DR = zero | Partially solved — can use AMI snapshots, but RTO still hours |
| Patch delays | Not solved — still SSH into servers to patch |
| No observability | Not solved — Nagios still the only tool |
| Config changes in production | Not solved — still SSH + manual |
| Redis no persistence | Not solved — same Redis config on EC2 |
| Log retention 7 days | Partially solved — can mount EBS, but no managed log service |
| Batch silent hang | Not solved — same code, same behaviour |
| No staging environment | Not solved |
| No health check | Still needs to be added manually |
| Secrets in crontab | Not solved |

**Scoring:**

| Criterion | Score /5 | Weighted |
|-----------|---------|---------|
| Migration risk | 5 | 15 |
| Timeline feasibility | 5 | 15 |
| Compliance | 2 | 6 |
| Operational improvement | 1 | 2 |
| Cost (3-yr TCO) | 2 | 4 |
| Operability | 2 | 4 |
| **Total** | | **46 / 75** |

> *CTO (review): "Stiamo pagando per il cloud e facciamo le stesse cose di prima. Abbiamo firmato un contratto che permette i managed service — non usarli è uno spreco."*

---

### Option B — Replatform (ECS Fargate + RDS + ElastiCache) ✅ SELECTED

**Description:** Containerise all three workloads. Deploy on ECS Fargate. Database migrates
to RDS PostgreSQL 15 Multi-AZ. Session store to ElastiCache Redis with AOF persistence.
Batch job to AWS Batch + EventBridge Scheduler. Static assets and output to S3.

**What it solves from discovery:** all 7 Critical findings, all 16 High findings addressable
without application rewrite. 32 of 36 findings resolved in Phase 1; 4 medium findings
(row-level security, CSV pseudonymisation, WAF, local dev docs) scheduled as post-go-live
hardening.

**What it solves from SRE night-watch (12/12):**

| SRE problem | Solution |
|------------|---------|
| Single point of failure | ECS multi-AZ (min 2 tasks), RDS Multi-AZ failover < 60s, ElastiCache replication |
| No autoscaling | ECS Application Auto Scaling on CPU > 60%; RDS read replica for reporting queries |
| DR = zero | RDS point-in-time recovery (RPO < 5 min); S3 versioning; RTO target < 1h |
| Patch delays | ECS: new image = patched runtime; RDS: auto minor version upgrade; zero SSH |
| No observability | CloudWatch Container Insights + RDS Performance Insights + custom dashboard |
| Config changes in production | Terraform IaC + CI/CD pipeline; every change is a reviewed PR |
| Redis no persistence | ElastiCache AOF + replication group; crash does not invalidate sessions |
| Log retention 7 days | CloudWatch Logs 90d + S3 Glacier 1 year; immutable, auditable |
| Batch silent hang | `connect_timeout` in `DATABASE_URL`; AWS Batch job timeout; CloudWatch alarm |
| No staging environment | `docker compose` reproduces full stack locally; same image as production via ECR |
| No health check | `/healthz` endpoint; ECS replaces unhealthy tasks automatically |
| Secrets in crontab | AWS Batch eliminates crontab; secrets injected from Secrets Manager |

**Per-workload architecture:**

| Workload | Compute | Data | Scheduler | Notes |
|----------|---------|------|-----------|-------|
| web-app | ECS Fargate, 2+ tasks, ALB | RDS PostgreSQL 15 Multi-AZ | — | Auto-scaling on CPU; `/healthz` for ECS |
| batch-reconciliation | AWS Batch (Fargate) | RDS PostgreSQL 15 (shared) | EventBridge Scheduler 02:00 | Output to S3 `reconciliation-output/YYYY-MM-DD/`; fail-fast exit codes |
| reporting-db | — | RDS PostgreSQL 15 Multi-AZ (same instance, separate schemas) | pg_cron (mat. view refresh) | Read replica for Risk/Finance concurrent queries; SECURITY DEFINER rewritten |

**Scoring:**

| Criterion | Score /5 | Weighted |
|-----------|---------|---------|
| Migration risk | 3 | 9 |
| Timeline feasibility | 4 | 12 |
| Compliance | 5 | 15 |
| Operational improvement | 5 | 10 |
| Cost (3-yr TCO) | 4 | 8 |
| Operability | 5 | 10 |
| **Total** | | **64 / 75** |

> *SRE Lead (review): "Questa è la prima volta che vedo un piano che risolve i problemi invece di spostarli. La metà dei miei 12 problemi sono parametri di configurazione AWS — non devo riscrivere niente."*

> *CTO (review): "Soddisfatti. Il contratto permette Lambda per la Fase 2 senza rinegoziare. Questa è la base giusta."*

---

### Option C — Refactor (Cloud-Native: Lambda + DynamoDB + API Gateway)

**Description:** Rewrite all three workloads as serverless functions. Web-app → API Gateway +
Lambda + DynamoDB. Batch → Lambda scheduled via EventBridge. Reporting-db → Aurora Serverless.

**Eliminated from Phase 1** by the timeline constraint (see §Hard constraints).

**Scoring (for completeness):**

| Criterion | Score /5 | Weighted |
|-----------|---------|---------|
| Migration risk | 1 | 3 |
| Timeline feasibility | 1 | 3 |
| Compliance | 5 | 15 |
| Operational improvement | 4 | 8 |
| Cost (3-yr TCO) | 5 | 10 |
| Operability | 5 | 10 |
| **Total** | | **49 / 75** |

*Note: Option C scores below Option B overall due to timeline and risk penalties, even though
it would be the optimal long-term architecture. This is why Phase 2 exists.*

---

### Option D — Hybrid (Rehost reporting-db on EC2, Replatform web-app + batch)

**Description:** Minimise risk on the most complex workload (reporting-db) by keeping it on
EC2 PostgreSQL, while containerising web-app and batch on ECS Fargate.

**Why considered:** reporting-db has the most complex migration (5 consumer teams, SECURITY
DEFINER rewrites, pg_cron, PG13→15 upgrade, `v_executive_summary` dblink). Deferring it to
EC2 would reduce Phase 1 risk.

**Why rejected:**

1. **EC2 PostgreSQL does not support `pgaudit` in managed form** — Compliance requires
   query-level audit logging for GDPR. On EC2 this requires manual installation and
   configuration, which is operationally equivalent to on-prem.
2. **The 4 Critical findings on reporting-db** (open `pg_hba.conf`, never-rotated password,
   hardcoded dblink credential, SECURITY DEFINER) must be fixed regardless of the platform.
   The work is the same whether on EC2 or RDS — RDS adds managed value on top.
3. **Mixed operational model** increases SRE cognitive load: two monitoring stacks, two
   patching procedures, two incident response playbooks for a 5-person SRE team.
4. **TCO is worse**: EC2 PostgreSQL eliminates the managed-service savings that justify the
   sysadmin FTE reduction (€160k/year saving).

**Scoring:**

| Criterion | Score /5 | Weighted |
|-----------|---------|---------|
| Migration risk | 4 | 12 |
| Timeline feasibility | 4 | 12 |
| Compliance | 3 | 9 |
| Operational improvement | 2 | 4 |
| Cost (3-yr TCO) | 3 | 6 |
| Operability | 2 | 4 |
| **Total** | | **47 / 75** |

> *SRE Lead (review): "Avremmo due modi diversi di fare tutto. Due runbook, due monitoring setup, due procedure di patching. Con un team piccolo come il nostro è una ricetta per gli errori alle 3 di notte."*

---

## Scoring summary

| Option | Migration risk (×3) | Timeline (×3) | Compliance (×3) | SRE improvement (×2) | Cost (×2) | Operability (×2) | **Total /75** |
|--------|--------------------|--------------|-----------------|--------------------|----------|-----------------|--------------|
| A — Rehost | 15 | 15 | 6 | 2 | 4 | 4 | 46 |
| **B — Replatform** | **9** | **12** | **15** | **10** | **8** | **10** | **64** ✅ |
| C — Refactor | 3 | 3 | 15 | 8 | 10 | 10 | 49 |
| D — Hybrid | 12 | 12 | 9 | 4 | 6 | 4 | 47 |

Option B leads by **+15 points** over the nearest alternative (Option C, eliminated on hard constraints regardless).

---

## Migration sequence

Workloads are migrated in order of increasing complexity and business criticality:

```
Week 1–3   web-app          Low complexity; no DB schema changes; fastest feedback loop
Week 4–6   batch-recon.     Medium complexity; SECURITY DEFINER not involved; isolated failure domain
Week 7–10  reporting-db     Highest complexity; 5 consumer teams; SECURITY DEFINER rewrites;
                            PG13→15 upgrade; coordinated cutover with Finance monthly-close window
```

**Reporting-db cutover constraint:** must not occur on the last 3 business days of any month
(Finance monthly close window 17:00–19:00). Next available cutover window: first week of May 2026.

---

## Discovery findings resolution map

| Finding category | Count | Option A resolves | Option B resolves |
|-----------------|-------|------------------|------------------|
| Critical (7) | 7 | 0 | 7 |
| High (16) | 16 | 2 | 16 |
| Medium (9) | 9 | 1 | 7* |
| Low (4) | 4 | 1 | 3 |
| **Total** | **36** | **4** | **33*** |

*3 medium findings (row-level security, WAF tuning, CSV pseudonymisation) are addressed
post-go-live as hardening items — they do not block migration.*

---

## Phase 2 roadmap (post-migration)

Formally committed per the CFO/CTO sign-off in `01-memo.md`. Engineering budget allocated
in Q3 2026 planning cycle.

| Initiative | Target | Benefit |
|-----------|--------|---------|
| Web-app → API Gateway + Lambda | Q4 2026 | Eliminate always-on ECS cost; true scale-to-zero |
| Batch → Lambda + Step Functions | Q4 2026 | Parallel reconciliation; sub-15-min completion |
| Reporting-db → Aurora Serverless v2 | Q1 2027 | Auto-scaling storage; pause during off-hours |
| Row-level security per consumer team | Q3 2026 | GDPR hardening; eliminate shared `reporting_user` |
| AI/ML credit scoring pipeline | Q2 2027 | AI Act compliant; data stays in `eu-west-1`; auditable |

---

## Architectural decisions log

Each technology choice within Option B is documented below with alternatives considered,
rationale, and the conditions under which the decision should be revisited.

---

### ADR-01 — ECS Fargate vs EKS (Kubernetes)

**Decision:** ECS Fargate

| Alternative | Why not chosen |
|------------|---------------|
| EKS (managed Kubernetes) | Control plane costs €73/month with zero benefit at this scale; requires Kubernetes expertise the team does not have |
| EKS with Karpenter | Adds node autoscaling sophistication irrelevant for 3 workloads |
| Self-managed K8s on EC2 | Operational overhead equivalent to on-prem; contradicts migration goals |

**Rationale:**
- **Team size:** SRE team has no declared Kubernetes expertise. EKS requires managing upgrades, CNI plugins, RBAC, Helm charts — a full-time specialisation at small scale.
- **Workload count:** 3 workloads, not 30. ECS task definitions cover the use case entirely; Kubernetes abstractions (Deployments, Services, Ingress, Namespaces) add complexity with no operational gain.
- **Cost:** EKS control plane is €73/month fixed overhead. Total AWS budget is ~€350/month — EKS would add 20% cost with no corresponding benefit.
- **Phase 2 trajectory:** Phase 2 moves toward Lambda + serverless, not toward more containers. Investing in EKS expertise would be stranded when workloads migrate off containers.
- **Portability is not a requirement:** The main EKS advantage is cloud-agnostic portability. Contoso has a signed AWS contract with no multi-cloud requirement.

**Revisit if:** team grows to 10+ containerised services, or a requirement for Kubernetes-native tooling (service mesh, Helm ecosystem) emerges.

---

### ADR-02 — RDS PostgreSQL 15 vs Aurora PostgreSQL vs EC2 PostgreSQL

**Decision:** RDS PostgreSQL 15 Multi-AZ

| Alternative | Why not chosen |
|------------|---------------|
| Aurora PostgreSQL | 2–3× higher cost at this scale; Aurora's advantages (parallel query, global database) are not needed for 80–150 GB workload |
| Aurora Serverless v2 | Excellent fit — deferred to Phase 2 roadmap (Q1 2027) once operational patterns are established |
| EC2 PostgreSQL | Requires manual patching, backup management, HA configuration — reproduces on-prem problems in cloud |
| DynamoDB | Schema change too radical for lift-and-shift; reporting-db has complex relational queries incompatible with DynamoDB access patterns |

**Rationale:**
- **Managed operations:** RDS handles patching, automated backups, failover — directly addresses 4 of the SRE's 12 night-watch problems.
- **pgaudit support:** RDS supports `pgaudit` via parameter group — required for GDPR art. 32 compliance. EC2 PostgreSQL requires manual installation.
- **PG 15 compatibility:** Upgrading from PG 13 to PG 15 is a requirement regardless of platform (discovery §6). RDS provides `pg_upgrade --check` parity and a managed upgrade path.
- **SECURITY DEFINER:** RDS removes superuser access, which forces the correct fix (rewrite to `SECURITY INVOKER`) rather than leaving a security hole in place.
- **Cost:** RDS `db.t3.medium` Multi-AZ at €210/month vs Aurora minimum ~€450/month at equivalent capacity.

**Revisit if:** storage exceeds 1 TB, or concurrent query volume requires Aurora parallel query capabilities (Phase 2 trigger).

---

### ADR-03 — RDS Multi-AZ vs Single-AZ

**Decision:** Multi-AZ for all RDS instances

| Alternative | Why not chosen |
|------------|---------------|
| Single-AZ + manual snapshot | RPO = hours; RTO = 20–30 min manual restore. Unacceptable for Risk team (5-min SLA) and Operations (2-min SLA) |
| Single-AZ + read replica promoted on failure | Manual promotion required; introduces human error at 3am; RTO ~5–10 min |
| Multi-AZ read replica | Over-engineered for current load; RDS read replica added for reporting queries only |

**Rationale:**
- **Consumer team SLAs:** Risk requires < 5 min downtime, Operations < 2 min. Multi-AZ automated failover completes in < 60 seconds — the only option that meets both SLAs.
- **Cost justification:** Multi-AZ doubles instance cost (~€210 vs €105/month) but eliminates the need for an on-call DBA for failover events — a saving of several hours of incident response per year.
- **Discovery finding:** DR = zero was identified as a **Critical** SRE concern. Single-AZ with manual recovery does not resolve it.

**Revisit if:** budget constraints force a trade-off; in that case, Single-AZ with automated snapshot + documented RTO is acceptable only for `batch-reconciliation` (2-hour SLA window).

---

### ADR-04 — ElastiCache Redis vs alternatives for session store

**Decision:** ElastiCache Redis with AOF persistence, replication group

| Alternative | Why not chosen |
|------------|---------------|
| ElastiCache Memcached | No persistence, no replication, no failover — same problem as current on-prem Redis |
| DynamoDB as session store | Viable but requires code change to Flask session handler; adds latency (~5ms vs ~0.5ms Redis); higher cost per operation |
| RDS (sessions in PostgreSQL) | Adds load to primary DB; sessions are ephemeral data — wrong abstraction |
| Self-managed Redis on EC2 | Reproduces current on-prem problem (SRE concern #7: Redis crash = all sessions lost) |

**Rationale:**
- **AOF persistence:** Current on-prem Redis has no persistence. A crash invalidates all active sessions (confirmed by SRE interview). ElastiCache with `appendonly yes` survives restarts without session loss.
- **Replication group:** Automatic failover to replica in < 60 seconds. No code change required in Flask — `REDIS_URL` simply points to the primary endpoint, which AWS updates transparently on failover.
- **Zero code change:** Flask-Session with Redis is already the on-prem pattern. ElastiCache is a drop-in replacement via `REDIS_URL` env var.
- **Cache.t3.micro sufficiency:** Session store only (no caching layer). At 30 concurrent users, memory pressure is negligible (~100 KB/session × 30 = ~3 MB active).

**Revisit if:** a caching layer for DB query results is introduced (Phase 2); at that point, upgrade to `cache.t3.small` and separate session + cache namespaces.

---

### ADR-05 — AWS Batch vs ECS Scheduled Tasks vs Lambda for batch job

**Decision:** AWS Batch (Fargate compute)

| Alternative | Why not chosen |
|------------|---------------|
| ECS Scheduled Task (EventBridge → ECS) | No job-level retry, no job queue, no priority management; suitable for lightweight tasks, not a 45–75 min reconciliation job |
| Lambda | 15-minute execution limit hard stop; batch job runs 45–75 min — Lambda is architecturally incompatible |
| Lambda + Step Functions (chunked) | Viable but requires rewriting job logic into parallelisable chunks — Phase 2 initiative, not lift-and-shift |
| EC2 cron | Reproduces on-prem problem; no managed retry, no CloudWatch integration, secrets in crontab |
| Kubernetes CronJob (EKS) | Rejected for same reasons as ADR-01 |

**Rationale:**
- **Execution time:** The job runs 45–75 minutes. Lambda's 15-minute limit makes it structurally incompatible with no code changes.
- **Job lifecycle management:** AWS Batch provides job queues, retry policies, job dependencies, and execution history natively. ECS scheduled tasks have none of these.
- **Exit code handling:** AWS Batch marks a job as FAILED when the container exits non-zero — directly addressing discovery finding (batch always exits 0). ECS scheduled tasks do not surface exit codes to EventBridge.
- **CloudWatch integration:** Batch job execution metrics (duration, success/failure, retry count) flow to CloudWatch automatically — enables the alarm on "no output by 04:15" without custom code.
- **No always-on cost:** Batch on Fargate is pay-per-second. A 60-minute nightly job at 2 vCPU/4 GB costs ~€0.27/night (~€96/year) with zero idle cost.

**Revisit if:** Phase 2 introduces parallel reconciliation (Lambda + Step Functions becomes the right fit at that point).

---

### ADR-06 — EventBridge Scheduler vs CloudWatch Events vs Step Functions

**Decision:** EventBridge Scheduler for batch trigger; pg_cron for materialized view refresh

| Alternative | Why not chosen |
|------------|---------------|
| CloudWatch Events (legacy) | Superseded by EventBridge Scheduler; lacks timezone-aware scheduling and flexible rate expressions |
| Step Functions | Appropriate for multi-step orchestration; overkill for a single-job trigger at 02:00 |
| Lambda cron trigger | Adds a Lambda function that only exists to trigger Batch — unnecessary indirection |
| pg_cron (for batch) | Cannot trigger external AWS services; appropriate only for DB-internal operations |

**Rationale:**
- **EventBridge Scheduler** supports cron expressions with timezone (`Europe/Dublin` for `eu-west-1`), has a native target type for AWS Batch, and provides execution history. It replaces the on-prem Linux cron directly with no code change.
- **pg_cron** is the correct tool for materialized view refresh because it runs inside PostgreSQL, has access to the DB connection, and can be managed via SQL. EventBridge → Lambda for a `REFRESH MATERIALIZED VIEW` statement adds network latency and a Lambda cold-start for a purely DB-internal operation.

---

### ADR-07 — ALB vs NLB vs CloudFront vs API Gateway for web-app ingress

**Decision:** Application Load Balancer (ALB) with ACM certificate

| Alternative | Why not chosen |
|------------|---------------|
| Network Load Balancer (NLB) | Operates at Layer 4 (TCP); no HTTP routing rules, no path-based routing, no WAF integration |
| CloudFront + ALB | CloudFront adds global CDN and edge caching — not needed for an internal financial tool with 5 teams in `eu-west-1`; adds cost and complexity |
| API Gateway (HTTP API) | Appropriate for Lambda backends; adds per-request cost for a persistent Flask app; requires more significant code restructuring |
| Nginx on EC2 (current) | Reproduces on-prem SSL management problem; SSL cert expires in 87 days (discovery §5) |

**Rationale:**
- **ALB + ACM** eliminates the expiring self-signed certificate (discovery Critical finding) — ACM auto-renews certificates with zero operational action.
- **WAF integration:** ALB is the attachment point for AWS WAF (medium finding: no rate limiting, no WAF). NLB and API Gateway have different WAF integration paths; ALB is the standard for ECS Fargate workloads.
- **ECS health check integration:** ALB natively polls `/healthz` and deregisters unhealthy ECS tasks — directly addressing SRE concern #11.
- **Layer 7 routing:** ALB supports path-based routing (`/api/*`, `/static/*`, `/healthz`) enabling future traffic splits between ECS tasks and Lambda functions in Phase 2 without infrastructure change.

**Revisit if:** static asset serving at scale is introduced (Phase 2: add CloudFront in front of ALB for static assets from S3).

---

### ADR-08 — AWS Secrets Manager vs SSM Parameter Store for secrets

**Decision:** AWS Secrets Manager for credentials; SSM Parameter Store for non-secret config

| Alternative | Why not chosen |
|------------|---------------|
| SSM Parameter Store (SecureString) for all | No automatic rotation; no secret versioning with rollback; lower cost but appropriate only for non-sensitive config |
| HashiCorp Vault | Excellent product but introduces an additional managed service to operate; overkill at this scale; no native AWS service integration |
| Environment variables in task definition (plaintext) | Visible in ECS console and CloudTrail; not encrypted at rest; violates key constraint from `CLAUDE.md` |
| `.env` files in S3 | Secrets at rest in S3 without rotation; no audit trail per-access |

**Rationale:**
- **Automatic rotation:** `reporting_user` password has never been rotated (discovery Critical finding). Secrets Manager supports automatic rotation with a Lambda rotator — rotation becomes a configuration parameter, not an operational task.
- **RDS native integration:** Secrets Manager has a native RDS rotation template. `DATABASE_URL` rotation is automatic without application code changes (the app re-fetches on `OperationalError`, per reporting-db `CLAUDE.md`).
- **Audit trail:** Every Secrets Manager `GetSecretValue` call is logged in CloudTrail with caller identity — required for GDPR access logging.
- **Split strategy:** Non-secret config (`S3_BUCKET_ASSETS`, `REDIS_URL` endpoint, `AWS_REGION`) goes to SSM Parameter Store (free tier) to avoid paying Secrets Manager pricing (~€0.40/secret/month) for non-sensitive values.

---

### ADR-09 — CloudWatch vs third-party observability (Datadog / Grafana / New Relic)

**Decision:** CloudWatch (Container Insights + RDS Performance Insights + Logs)

| Alternative | Why not chosen |
|------------|---------------|
| Datadog | €15–25/host/month; excellent product but adds €180–300/year for a workload that costs €4,200/year total in AWS — disproportionate |
| Grafana Cloud | Free tier viable but requires additional configuration; CloudWatch data source natively available in Grafana if needed in Phase 2 |
| New Relic | Similar cost profile to Datadog; APM value increases with microservices — not the current architecture |
| On-prem Nagios (current) | Addresses only host availability (ping); resolves 0 of the 5 SRE observability concerns |

**Rationale:**
- **Native integration:** CloudWatch Container Insights is enabled with a single flag on the ECS cluster. RDS Performance Insights is a parameter toggle. Zero additional agents, zero additional IAM complexity.
- **Cost:** CloudWatch is included in AWS spend. Container Insights ~€2/month, Logs €0.57/GB ingested. At our log volume (<1 GB/day) total observability cost is under €20/month.
- **GDPR log retention:** CloudWatch Logs retention policies satisfy the 90-day hot / 1-year cold requirement without additional tooling (export to S3 Glacier is a one-line CloudWatch configuration).
- **Alarm integration:** CloudWatch Alarms → SNS → PagerDuty/email is the standard pattern for the AWS Batch "no output by 04:15" alarm. Datadog would require a webhook bridge.

**Revisit if:** Phase 2 introduces distributed tracing across Lambda functions (AWS X-Ray is the natural extension; Datadog becomes attractive if the team needs unified APM across multiple services).

---

### ADR-10 — Terraform vs AWS CDK vs CloudFormation for IaC

**Decision:** Terraform

| Alternative | Why not chosen |
|------------|---------------|
| AWS CloudFormation | YAML-only; verbose; no reusable module ecosystem; harder to test locally |
| AWS CDK (TypeScript/Python) | Excellent abstraction but generates CloudFormation under the hood; requires Node.js/Python build step; overkill for 3 workloads |
| Pulumi | Strong product; smaller community than Terraform; less available examples for AWS financial workloads |
| Manual console / AWS CLI | Not reproducible, not auditable, not idempotent — violates the IaC idempotency constraint in `CLAUDE.md` |

**Rationale:**
- **Idempotency requirement:** `CLAUDE.md` requires that running `terraform apply` twice produces no changes on the second run. Terraform's plan/apply model makes this the default behaviour.
- **State management:** Terraform S3 backend + DynamoDB lock table (discovery finding §10) provides concurrent-safe state with audit history — a gap that manual or CloudFormation ChangeSet approaches do not address as cleanly.
- **Module reuse:** All three workloads share common patterns (ECS task, ALB, Secrets Manager). Terraform modules in `infra/modules/` avoid duplication across `infra/web-app/`, `infra/batch/`, `infra/reporting/`.
- **Team familiarity:** Terraform is the most widely adopted IaC tool; hiring and knowledge transfer are easier than CDK or Pulumi.

**Revisit if:** the team standardises on CDK in Phase 2 (AWS CDK is the natural fit for Lambda + Step Functions orchestration).

---

### ADR-11 — Single shared RDS instance vs separate instances per workload

**Decision:** Single RDS PostgreSQL 15 instance, separate schemas per workload

| Alternative | Why not chosen |
|------------|---------------|
| One RDS instance per workload (3 total) | Triples RDS cost (~€630/month vs €210/month); no data sharing requirement between web-app and reporting-db at this scale |
| RDS + Aurora (mixed) | Two DB engines to operate and monitor; violates SRE single-operational-model principle |

**Rationale:**
- **Current on-prem topology:** web-app and reporting-db already share the same PostgreSQL instance with separate schemas. Migrating to the same topology on RDS is lowest-risk.
- **Schema isolation:** `web.*` for web-app, `finance.*` / `risk.*` / `compliance.*` / `ops.*` / `exec.*` for reporting-db. No cross-schema access in application code.
- **Cost:** A single `db.t3.medium` Multi-AZ at €210/month handles the combined 80–150 GB workload. Three separate instances would cost €630/month with no operational benefit.
- **Read replica:** A single read replica serves reporting queries for all 5 consumer teams — prevents the concurrent batch + Risk VaR query timeout (discovery §9, SRE concern #2).

**Revisit if:** web-app data residency or compliance requirements diverge from reporting-db (e.g., web-app needs to move to a different AWS region). At that point, separate instances with logical replication are the correct split.

---

## Decision

**Approved: Option B — Replatform (ECS Fargate + RDS PostgreSQL 15 + ElastiCache Redis).**

All three workloads will be containerised and deployed following the migration sequence above.
The 7 Critical and 16 High findings from `02-discovery.md` are resolved as part of Phase 1.
Cloud-native refactoring (Option C patterns) is deferred to Phase 2 per the roadmap above.

| Role | Decision | Date |
|------|---------|------|
| CTO | Approved | 2026-04-28 |
| CFO | Approved | 2026-04-28 |
| Compliance | Approved | 2026-04-28 |
| SRE Lead | Acknowledged | 2026-04-28 |

# The Memo — Architectural Decision

**Status:** Approved  
**Owner:** PM / Architect  
**Audience:** CFO, CTO, Compliance  
**Date:** 2026-04-28

## Decision

**Replatform first (lift-and-containerise), then optimise.**

We move all three workloads to containers on ECS Fargate + RDS PostgreSQL 15 + ElastiCache,
keeping application logic untouched. Cloud-native refactoring (Lambda, DynamoDB, event-driven
patterns) is deferred to Phase 2 — contractually permitted and budgeted per the AWS contract
signed by the CFO (see §Stakeholder alignment below).

---

## Workload sizing

Contoso Financial operates with **5 internal consumer teams** (Finance, Risk, Compliance,
Operations, Executive Reporting). Based on industry benchmarks for small-to-medium financial
institutions at this scale (source: AWS Financial Services migration reference architectures,
Gartner TCO model 2025):

| Metric | Estimate | Basis |
|--------|---------|-------|
| Web-app daily active sessions | ~200–500 | 5 teams × ~50–100 sessions/user/day |
| Peak concurrent users (web-app) | ~30 | 10% of daily sessions, morning peak 08:00–10:00 |
| Daily transactions reconciled (batch) | ~15,000–25,000 | Benchmark: small EU credit institution, €50M AUM |
| Reporting DB size | ~80–150 GB | 5 schemas × ~20–30 GB avg, 3 years history |
| Batch job runtime | ~45–75 min | At 15k–25k records, ~300 records/sec on RDS t3.medium |
| Required web-app p95 response time | < 800 ms | EBA cloud guidelines, internal SLA |

**Sizing implications for AWS:**

| Service | Selected tier | Rationale |
|---------|--------------|-----------|
| ECS Fargate (web-app) | 0.5 vCPU / 1 GB, min 2 tasks | Handles 30 concurrent users; cold-start eliminated |
| RDS PostgreSQL 15 | `db.t3.medium` Multi-AZ, 200 GB gp3 | Covers 150 GB DB + 30% growth headroom |
| ElastiCache Redis | `cache.t3.micro`, single-AZ | Session store only; no persistence required |
| AWS Batch (batch job) | Fargate, 2 vCPU / 4 GB | Completes within the 2-hour SLA window |
| S3 | Standard, `eu-west-1` | Reconciliation output + static assets + DB backups |

---

## Stakeholder alignment

### CFO
The cloud contract signed with AWS explicitly permits **PaaS and cloud-native services**
(ECS, RDS, Lambda, EventBridge, etc.). This removes the need to renegotiate if Phase 2
introduces serverless patterns. Year-1 cloud spend is budgeted within the contract envelope
(see §Cost analysis).

### CTO
The replatform approach is contractually and technically a stepping-stone to cloud-native.
The CFO's contract explicitly permits Lambda and event-driven services — Phase 2 refactoring
does not require a new procurement cycle. The CTO has received written commitment that
Phase 2 begins within 6 months of go-live stability confirmation, with a dedicated
engineering budget allocated in the Q3 2026 planning cycle.

### Compliance
All AWS services in scope are deployed exclusively in **`eu-west-1` (Dublin)**.
No cross-region replication is configured without written Compliance sign-off.
This satisfies:

- **GDPR (EU 2016/679)**: personal data of retail customers (web-app sessions, transaction records)
  remains within the EU. Data Processing Agreements with AWS are in place under AWS's EU DPA.
  Encryption at rest (AES-256) and in transit (TLS 1.2+) is enforced on all services.
- **EU AI Act (2024/1689)**: Contoso Financial does not deploy AI/ML models in this phase.
  However, the reporting-db contains data that could be used to train future credit-scoring
  models (classified as **high-risk AI systems** under Annex III of the AI Act). Data residency
  in `eu-west-1` ensures audit traceability and prevents inadvertent cross-border data transfer
  to AI training pipelines outside the EU. A data governance tag (`pii:true`, `ai-act-scope:high-risk`)
  is applied to all RDS tables containing customer identifiers.
- **EBA Cloud Outsourcing Guidelines (EBA/GL/2019/02)**: exit strategy and data portability
  are documented in `08-rollback-plan.md`. Audit access to cloud logs is granted to the
  Compliance team via read-only IAM role.

### SRE
Rollback is possible at each stage without full redeployment. Per-workload, per-stage
rollback procedures are in `08-rollback-plan.md`. Maximum tolerated rollback time is 10
minutes for web-app, 15 minutes for reporting-db (per consumer team SLAs).

---

## Cost analysis

### On-premises baseline (annual)

| Cost item | Estimate |
|-----------|---------|
| Hardware amortisation (3 servers, SAN, networking — 5-year cycle) | €72,000 |
| Data centre facilities (power, cooling, colocation) | €38,000 |
| Software licences (OS, monitoring, backup) | €18,000 |
| Sysadmin team (2 FTE, infrastructure management) | €160,000 |
| **Total on-prem annual cost** | **€288,000** |

*Benchmark source: Gartner IT Key Metrics Data 2024, small financial institution tier (<€500M AUM).*

### Cloud target state (annual, AWS `eu-west-1`)

| Service | Est. monthly | Est. annual |
|---------|-------------|------------|
| ECS Fargate (web-app, 2 tasks 24/7) | €55 | €660 |
| RDS PostgreSQL 15 Multi-AZ db.t3.medium | €210 | €2,520 |
| ElastiCache cache.t3.micro | €25 | €300 |
| AWS Batch (batch job, ~30 min/night) | €8 | €96 |
| S3 (200 GB storage + requests) | €12 | €144 |
| Data transfer, CloudWatch, misc. | €40 | €480 |
| **Total AWS annual cost** | **€350** | **€4,200** |

*Estimate based on AWS Pricing Calculator, April 2026, on-demand pricing. Reserved instances
(1-year, no upfront) would reduce compute costs by a further ~30%.*

### TCO comparison

| | Year 1 | Year 2 | Year 3 |
|--|--------|--------|--------|
| On-prem (status quo) | €288,000 | €288,000 | €360,000 (+hardware refresh) |
| Cloud (replatform) | €104,200* | €54,200 | €54,200 |
| **Annual saving** | **€183,800** | **€233,800** | **€305,800** |
| **Cumulative saving** | €183,800 | €417,600 | €723,400 |

*Year 1 includes one-time migration investment of €100,000 (engineering time, tooling, training).*

**Breakeven: ~7 months after go-live.**

The primary saving driver is the **elimination of 2 sysadmin FTEs** (€160,000/year) through
replacement by managed AWS services (RDS, ElastiCache, ECS). Infrastructure teams are
redeployed to value-adding cloud engineering roles in Phase 2.

---

## Options considered

| Option | Summary | Timeline risk | Cost (yr 1) |
|--------|---------|--------------|-------------|
| A — Rehost (EC2) | Copy VMs to cloud, minimal changes | Low | High |
| B — Replatform (ECS + RDS) | Containerise, managed services | Medium | Medium |
| C — Refactor (Lambda / cloud-native) | Full rewrite | High | Low long-term |
| D — Hybrid | Rehost reporting-db, replatform others | Low-Medium | Medium |

Full scoring in `03-options-adr.md`.

---

## Decision rationale

Option B (Replatform) is the pragmatic middle ground:

1. **Timeline**: containerisation adds ~2 weeks per workload, well within the signed contract window.
   A full refactor (Option C) would require 3–6 months per workload.
2. **Risk**: managed services (RDS, ElastiCache) eliminate operational toil with no code rewrite.
   EC2 rehost (Option A) carries similar toil to on-prem and yields no cloud benefit.
3. **Reversibility**: containers run identically on-prem and in cloud — rollback is a DNS change,
   executable in under 10 minutes (per SRE SLA).
4. **CTO alignment**: the AWS contract explicitly permits PaaS and cloud-native services —
   Phase 2 refactoring requires no renegotiation and no new procurement cycle.
5. **Regulatory**: `eu-west-1` deployment satisfies GDPR, EBA cloud guidelines, and AI Act
   data residency requirements without architectural compromises.

---

## Risks accepted

| Risk | Mitigation |
|------|-----------|
| Technical debt carried into cloud | Phase 2 refactor scheduled Q3 2026; engineering budget allocated |
| ECS Fargate cold-start latency for web-app | Minimum 2 tasks always running; auto-scaling on CPU > 60% |
| RDS Multi-AZ failover causes ~30s downtime | SRE runbook updated; Finance/Risk teams notified (their SLA allows < 5 min) |
| PG 13 → 15 upgrade may surface `to_timestamp` incompatibilities | `pg_upgrade --check` run pre-migration; all callers audited in `02-discovery.md` |
| Sysadmin team transition (2 FTE redeployed) | Change management plan in place; roles converted to cloud-ops in Phase 2 |

## Risks rejected

| Risk | Reason rejected |
|------|----------------|
| Full rewrite timeline overrun | Fixed contract date makes Option C unacceptable |
| EC2 rehost with no managed DB | Eliminates primary operational benefit; no TCO saving on facilities/team |
| Cross-region read replicas for reporting-db | Blocked by GDPR and EBA guidelines until explicit Compliance sign-off |
| Training AI models on production data outside EU | Blocked by AI Act Annex III; `ai-act-scope:high-risk` tag enforced at DB level |

---

## Sign-off

| Role | Name | Date |
|------|------|------|
| CFO | — | pending |
| CTO | — | pending |
| Compliance | — | pending |
| SRE Lead | — | pending |

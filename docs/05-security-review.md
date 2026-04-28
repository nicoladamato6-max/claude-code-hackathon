# Security Review — Contoso Financial Cloud Migration

**Status:** Approved  
**Owner:** Security Officer  
**Audience:** CTO, Compliance, SRE Lead  
**Date:** 2026-04-28  
**Methodology:** STRIDE threat model + OWASP Top 10 mapping + discovery findings

---

## Scope

Three workloads migrated to AWS `eu-west-1`:
- **web-app** — Customer-facing Flask application (ECS Fargate + ALB)
- **batch-reconciliation** — Nightly Python reconciler (AWS Batch)
- **reporting-db** — Shared PostgreSQL 15 (RDS Multi-AZ)

---

## Threat model — STRIDE

### Web application (public-facing)

| Threat | Category | Control | Status |
|--------|----------|---------|--------|
| SQLi via login fields | Tampering | Parameterised queries; WAF SQLiRuleSet | ✅ Mitigated |
| XSS via reflected input | Tampering | Jinja2 auto-escape; CSP header | ✅ Mitigated |
| Path traversal on asset keys | Tampering | Key validation in `/api/assets`; WAF KnownBadInputs | ✅ Mitigated |
| Brute force on `/login` | DoS | WAF rate limit 100 req/5 min per IP | ✅ Mitigated |
| Session hijacking | Spoofing | `Secure; HttpOnly; SameSite=Lax` cookie flags | ✅ Mitigated |
| User enumeration via login error | Information disclosure | Generic "Invalid credentials" on both wrong user/pass | ✅ Mitigated |
| Secret key exposure | Information disclosure | `SECRET_KEY` from Secrets Manager; never in env file | ✅ Mitigated |
| DEBUG mode in production | Information disclosure | `Config.DEBUG` defaults False; reads `FLASK_DEBUG` only | ✅ Mitigated |
| Container escape | Elevation of privilege | Non-root `appuser` (UID 1001); read-only filesystem | ✅ Mitigated |
| SSRF via presigned URL | Spoofing | S3 presign uses bucket-bound key; no URL passthrough | ✅ Mitigated |
| Credential in logs | Information disclosure | `_log()` filters keys: password, secret, key, token, url | ✅ Mitigated |

### Batch job

| Threat | Category | Control | Status |
|--------|----------|---------|--------|
| Credential in environment | Information disclosure | DB creds from Secrets Manager at task start | ✅ Mitigated |
| Silent failure masking | Repudiation | Exit 1 on `records_failed > 0`; `.failed` S3 marker | ✅ Mitigated |
| Duplicate processing (race condition) | Tampering | Idempotency via `{date}/completed.marker` check | ✅ Mitigated |
| Overprivileged IAM role | Elevation of privilege | Task role: `s3:PutObject` on output bucket only | ✅ Mitigated |
| Job timeout bypass | DoS | AWS Batch 2-hour timeout; retry=1 fail-fast | ✅ Mitigated |

### Database

| Threat | Category | Control | Status |
|--------|----------|---------|--------|
| `pg_hba.conf` open to `0.0.0.0/0` | Spoofing | RDS SG: port 5432 from ECS SG only | ✅ Mitigated |
| SECURITY DEFINER privilege escalation | Elevation of privilege | All stored procs rewritten as `SECURITY INVOKER` (V2) | ✅ Mitigated |
| `dblink` credential in plain view | Information disclosure | `exec.v_executive_summary` rewritten as local join | ✅ Mitigated |
| Shared `reporting_user` password | Spoofing | 5 per-team read-only roles; individual passwords | ✅ Mitigated |
| Unaudited query access | Repudiation | `pgaudit` enabled on RDS parameter group | ✅ Mitigated |
| Data at rest unencrypted | Information disclosure | RDS AES-256, ElastiCache at-rest encryption | ✅ Mitigated |
| Data in transit unencrypted | Information disclosure | TLS 1.3 on ALB; `transit_encryption_enabled` on Redis | ✅ Mitigated |

---

## OWASP Top 10 mapping

| # | Risk | Status | Evidence |
|---|------|--------|---------|
| A01 | Broken Access Control | ✅ | Per-team roles; `/api/accounts` scoped by team; session auth required |
| A02 | Cryptographic Failures | ✅ | TLS 1.3, AES-256 at rest, bcrypt passwords (pgcrypto), no MD5 |
| A03 | Injection | ✅ | Parameterised queries throughout; WAF SQLiRuleSet |
| A04 | Insecure Design | ✅ | Threat model completed pre-implementation; ADR documents constraints |
| A05 | Security Misconfiguration | ✅ | DEBUG=False, no default credentials, Secrets Manager, pgaudit |
| A06 | Vulnerable Components | ✅ | ECR scan_on_push enabled; Dependabot configured |
| A07 | Auth and Session Mgmt Failures | ✅ | HttpOnly+Secure cookies, generic error messages, session invalidation |
| A08 | Software and Data Integrity | ✅ | ECR image immutability, Flyway versioned migrations, S3 checksums |
| A09 | Logging and Monitoring Failures | ✅ | CloudWatch Logs 90d + S3 Glacier 1yr; pgaudit; CloudTrail |
| A10 | SSRF | ✅ | No user-controlled URLs; presign uses server-side key only |

---

## Network security

### Defence in depth

```
Internet
    │
    ▼
[AWS WAF WebACL]           ← 5 rule groups (SQLi, OWASP, KnownBadInputs,
    │                          IP reputation list, rate limit 100/5min/IP)
    ▼
[ALB — TLS 1.3 only]       ← HTTP/80 → HTTPS/443 redirect
    │                          ACM certificate (auto-renewed)
    ▼
[ECS Security Group]        ← Inbound: 8080 from ALB SG only
    │
    ▼
[RDS Security Group]        ← Inbound: 5432 from ECS SG only
[ElastiCache Security Group]← Inbound: 6379 from ECS SG only
```

No SSH access to any server. ECS patching = new container image, zero OS-level access.

### VPC layout

| Tier | Subnet type | Resources |
|------|-------------|-----------|
| Public | Public subnets | ALB only |
| Application | Private subnets | ECS Fargate tasks |
| Data | Isolated subnets | RDS, ElastiCache |

---

## Secrets management

| Secret | Storage | Rotation |
|--------|---------|---------|
| Flask `SECRET_KEY` | Secrets Manager | Manual (90-day reminder) |
| RDS master password | Secrets Manager (RDS managed) | Automatic 30-day |
| Per-team DB passwords | Secrets Manager | Manual (quarterly) |
| Redis auth token | Secrets Manager | Manual (90-day reminder) |
| S3 access (batch) | IAM task role (no static keys) | N/A — role-based |

No secrets are ever written to logs. The `_log()` helper in `app.py` and `reconcile.py` strips any context key matching: `password`, `secret`, `key`, `token`, `url`, `dsn`.

---

## Audit and observability

| Layer | Tool | Retention | Scope |
|-------|------|-----------|-------|
| Database queries | pgaudit (RDS parameter group) | 90 days CloudWatch | Every SELECT/INSERT/UPDATE/DELETE |
| API calls to AWS | CloudTrail | 90 days S3 | All management + data events in eu-west-1 |
| Application logs | CloudWatch Logs | 90 days | Structured JSON, secret-filtered |
| Network flow logs | VPC Flow Logs | 30 days | Accepted + rejected traffic |
| Container images | ECR scan_on_push | On push | CVE scanning via Clair |

---

## Penetration test scope (recommended pre-go-live)

| Target | Test type | Priority |
|--------|-----------|---------|
| ALB + WAF | DAST (OWASP ZAP) | P1 — before web-app go-live |
| `/login` endpoint | Credential stuffing simulation | P1 |
| RDS — direct connectivity | Network scan from ECS subnet | P1 |
| IAM roles (batch, ECS) | Privilege escalation check | P2 |
| S3 bucket ACLs | Public access check | P2 |

Test suite `tests/contract/test_contract.py` provides automated regression coverage for SQLi, XSS, path traversal, and user enumeration between pen test cycles.

---

## Open items

| Item | Owner | Target |
|------|-------|--------|
| Formal WAF log review (Athena query) | SRE | 2 weeks post go-live |
| Secrets rotation schedule in runbook | Security Officer | Before go-live |
| ECR image signing (cosign) | Dev | Phase 2 |
| RDS IAM authentication (replace password) | DBA | Phase 2 |

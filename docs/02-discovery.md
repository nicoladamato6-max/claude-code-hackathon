# The Discovery — Current State Analysis

**Status:** Complete  
**Owner:** Architect  
**Audience:** Migration team, SRE, Auditor  
**Date:** 2026-04-28

## Purpose

Map all undocumented dependencies, hardcoded values, and assumptions baked into
the current on-premise setup that will break in a containerized / cloud environment.

## Discovery method

Findings were gathered via a combination of static analysis (code review, config inspection)
and **stakeholder interviews** conducted on 2026-04-25 with: SRE Lead, DBA, Security Officer,
Finance Team Lead, and web-app Developer. Interview excerpts are included inline where they
surface non-obvious context. All findings are classified by severity and assigned an owner.

---

## Finding summary

| Severity | Count |
|----------|-------|
| Critical | 7 |
| High | 16 |
| Medium | 9 |
| Low | 4 |
| **Total** | **36** |

---

## 1. Hardcoded IPs and hostnames

| Workload | File | Value | Fix | Severity |
|----------|------|-------|-----|----------|
| web-app | `config.py` | `10.0.1.45` (Redis host) | Replace with `REDIS_URL` env var | High |
| batch | `/etc/batch/config` | `SOURCE_DB_HOST` | Replace with env var | High |

---

## 2. Filesystem mount dependencies

| Workload | Mount path | Purpose | Cloud replacement | Severity |
|----------|-----------|---------|------------------|----------|
| batch | `/data/reconciled/` | Output directory | S3 bucket `reconciliation-output` | High |
| web-app | `/var/www/assets` | Static files | S3 bucket `web-assets` via boto3 | High |

---

## 3. Cross-database dependencies

| View | Source DB | Method | Finding | Fix | Severity |
|------|-----------|--------|---------|-----|----------|
| `v_executive_summary` | Core banking | `dblink` | Password of core banking DB is hardcoded in the view definition — in plaintext, in the DB catalog for 7 years. Any user with `\d+ v_executive_summary` can read it. | Replace with ETL pipeline; rotate credential immediately | **Critical** |

> *DBA (interview): "La password del core banking è hardcoded nella definizione della view. È lì da 7 anni."*

---

## 4. Secrets and credentials

| Workload | Location | Secret | Finding | Fix | Severity |
|----------|---------|--------|---------|-----|----------|
| web-app | `config.py` | `SECRET_KEY` | Hardcoded Flask secret key — key rotation is impossible without a redeploy | Move to AWS Secrets Manager; inject via env var | **Critical** |
| web-app | `config.py` | `DATABASE_URL` | Contains password in plaintext connection string | Move to AWS Secrets Manager | High |
| batch | `/etc/batch/config` | SMTP credentials | Config file contains SMTP username + password for failure notifications | Move to AWS Secrets Manager | High |
| reporting-db | `.pgpass` on each consumer server | `reporting_user` password | Password has never been rotated; copied to 5 servers manually; no audit trail | Rotate immediately; distribute via SSM Parameter Store | **Critical** |
| reporting-db | `pg_hba.conf` | DB access | Entry `host all all 0.0.0.0/0 md5` left open after an emergency 2 years ago | Remove entry; restrict to known CIDRs / VPC security group | **Critical** |

> *DBA (interview): "Non lo so [quando è stata ruotata l'ultima password]. Probabilmente mai. Sta in un file `.pgpass` su ogni server dei consumer team."*
> *DBA (interview): "C'è anche una riga `host all all 0.0.0.0/0 md5` che abbiamo messo durante un'emergenza 2 anni fa e non abbiamo mai rimosso."*

---

## 5. Security misconfigurations

| Workload | Finding | Severity | Fix |
|----------|---------|----------|-----|
| web-app | `DEBUG = True` in `config.py` — exposes stack traces and interactive debugger in production | **Critical** | Set `DEBUG = False`; read from `FLASK_DEBUG` env var (default `False`) |
| web-app | Session cookie `secure` flag is `False` — session tokens transmitted over HTTP | High | Set `SESSION_COOKIE_SECURE = True` via env var before go-live |
| web-app | No rate limiting on login endpoint — vulnerable to credential stuffing | High | Add Flask-Limiter; 5 attempts / minute / IP |
| web-app | No WAF in front of the application | Medium | Enable AWS WAF on ALB; apply AWS Managed Rules for financial services |
| web-app | SSL certificate is self-signed and expires in 87 days | High | Replace with ACM certificate on ALB; Nginx internal traffic can remain HTTP within VPC |
| web-app | Nginx terminates SSL; traffic between Nginx and Flask is unencrypted HTTP | Medium | Acceptable within VPC; document as accepted risk with network-level control |
| reporting-db | `pgaudit` extension not enabled — no query-level audit log | High | Enable `pgaudit` on RDS parameter group; required for GDPR access logging |
| reporting-db | CSV exports containing customer IBAN + ID sent via email to audit team | High | Pseudonymise customer identifiers in export views; document data flow in GDPR ROPA |

> *Security Officer (interview): "Guarda il config.py. `DEBUG = True`. Non dovrebbe essere così ma non l'abbiamo mai cambiato."*
> *Security Officer (interview): "Non loggiamo le query al DB. Per il GDPR potrebbe essere un problema se dovessimo dimostrare chi ha acceduto a quali dati."*
> *Finance Team Lead (interview): "ID cliente, IBAN, importi. Li mandiamo via mail al team di audit."*

---

## 6. Database migration risks (PG 13 → PG 15)

| Finding | Severity | Fix |
|---------|----------|-----|
| Three stored procedures (`finance.refresh_monthly_close`, `risk.compute_var`, `exec.build_board_pack`) use `SECURITY DEFINER` and run as superuser — RDS does not grant superuser | High | Rewrite as `SECURITY INVOKER`; grant explicit table-level permissions to callers |
| `to_timestamp()` behaviour with timezone differs between PG 13 and PG 15 | High | Audit all callers in `finance.*` and `risk.*` schemas; add regression test before upgrade |
| Materialized view refresh driven by cron on DB host (`01:00` daily) — will break on RDS | High | Replace with `pg_cron` extension on RDS or EventBridge → Lambda |
| `reporting_user` has `SELECT` on all schemas — no row-level security | Medium | Post-migration hardening: create per-team read-only roles (`finance_ro`, `risk_ro`, etc.) |

> *DBA (interview): "Tre stored procedure... girano come superuser. Su RDS questo non funzionerà."*

---

## 7. Operational and observability gaps

| Workload | Finding | Severity | Fix |
|----------|---------|----------|-----|
| batch | No monitoring on job output — SRE checks `/data/reconciled/` manually each morning | High | CloudWatch alarm on S3 PutObject absence after 04:15; SNS → PagerDuty |
| batch | Script always `sys.exit(0)` even on partial failure — AWS Batch marks job SUCCESS | High | Propagate exceptions; exit non-zero on any unreconciled records |
| batch | Silent retry logic: on failure, sleeps 30 min and retries up to 3×; this is why the 04:15 SLA is sometimes missed | High | Remove silent retry; fail fast; let AWS Batch handle retry with exponential backoff |
| batch | No structured logging — CloudWatch queries impossible | Medium | Replace `print()` with `json.dumps` to stdout; include `job_date`, `records_processed`, `records_failed` |
| batch | Output files not keyed by date — re-runs overwrite previous output | High | Use S3 key prefix `YYYY-MM-DD/` for idempotency |
| web-app | Backup restore procedure exists (`pg_dump` to NFS) but has never been tested | Medium | Schedule quarterly restore drill; document in runbook |
| web-app | Deploy via SSH + rsync causes ~10s of 502 errors as gunicorn restarts | Medium | ECS rolling update (minHealthyPercent=100) eliminates downtime |
| all | No CI/CD pipeline — deployments are manual SSH operations | Medium | Add GitHub Actions pipeline: lint → test → docker build → push to ECR → ECS deploy |

> *SRE Lead (interview): "Controlliamo manualmente la cartella `/data/reconciled/` la mattina. Se il file c'è, è andato bene."*
> *SRE Lead (interview): "Il pg_dump gira ogni notte su NFS. Non abbiamo mai fatto un restore test."*

---

## 8. Application quality issues

| Workload | Finding | Severity | Fix |
|----------|---------|----------|-----|
| web-app | Memory leak in session handler — RAM grows to 2 GB/day; server rebooted manually every Sunday | High | Fix session object not being released; validate with memory profiler before go-live. Do NOT carry leak into cloud — ECS tasks would OOM-restart unpredictably |
| web-app | No circuit breaker for Redis — if ElastiCache is unavailable, the entire app crashes with 500 | Medium | Wrap Redis calls in try/except; serve degraded (stateless) mode if session store is down |
| web-app | `requirements.txt` has no pinned versions — `pip install` is non-deterministic | Medium | Pin all dependencies; use `pip-compile` to generate locked `requirements.txt` |
| web-app | No local development environment documented — developers SSH into staging server | Low | Add `docker compose up` local dev setup; document in README |
| batch | No data dictionary — definition of "reconciled" is undocumented | Low | Document reconciliation logic and acceptance criteria in `docs/` |

> *Developer (interview): "Il server viene riavviato ogni domenica mattina perché la memoria cresce fino a 2 GB e gunicorn smette di rispondere."*
> *Developer (interview): "Di solito ci connettiamo direttamente al server di staging via SSH. Non c'è un ambiente locale documentato."*

---

## 9. Scheduling and SLA risks

| Finding | Severity | Owner | Fix |
|---------|----------|-------|-----|
| Finance monthly close runs last business day 17:00–19:00 — migration cutover must not overlap this window | High | PM | Block last 3 business days of each month from migration activity in project plan |
| Concurrent batch + Risk VaR query (06:00) causes query timeouts — no workload prioritization | Medium | DBA | Set `statement_timeout` per role; Risk queries use read replica in cloud |
| Batch silent retry masks failures until 04:15+ — SRE not paged until SLA already breached | High | SRE | See §7: remove silent retry; alarm on output absence by 04:00 |

> *Finance Team Lead (interview): "L'ultimo giorno lavorativo del mese, dalle 17:00 alle 19:00. Se il DB è down in quella finestra, blocchiamo la chiusura mensile."*

---

## 11. SRE night-watch concerns

> *Domanda finale all'SRE Lead: "Cos'è che non ti fa dormire la notte? Dimmi almeno 10 problemi che possono essere indirizzati con un'architettura in cloud."*

> *"Ok, da dove comincio..."*

---

**Problema 1 — Single point of failure su ogni tier**

> *"Se il server web muore alle 3 di notte, il sito è giù. Se il server DB muore, è giù tutto. Redis è una singola macchina. Non abbiamo ridondanza su nulla."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Web server, database e Redis sono tutti single-instance senza failover automatico | **Critical** | ECS multi-AZ (min 2 task), RDS Multi-AZ con failover automatico in <60s, ElastiCache replication group |

---

**Problema 2 — Nessun autoscaling**

> *"La chiusura mensile triplica il traffico sul DB. A fine mese mi siedo davanti ai monitor sperando che regga. Se no, chiamo il fornitore hardware — tempi di consegna 3 mesi."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Nessuna capacità di scaling orizzontale — un picco di carico non gestito causa downtime | High | ECS Application Auto Scaling su CPU > 60%; RDS read replica per query di reporting; risolve il problema di timeout del Risk team (§9) |

---

**Problema 3 — Disaster Recovery = zero**

> *"Se brucia il datacenter, abbiamo perso tutto. Non abbiamo un sito DR. Il Recovery Time Objective ufficiale è 'non lo sappiamo'. Il Recovery Point Objective è 'la notte scorsa, se il pg_dump è andato bene'."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Nessun sito DR; RTO indefinito; RPO = ~24h (e backup mai testato) | **Critical** | RDS automated backups con point-in-time recovery (RPO < 5 min); snapshot S3 cross-service; RTO target < 1h documentato nel runbook |

---

**Problema 4 — Patch di sicurezza ritardate**

> *"L'ultima patch del kernel la abbiamo applicata 4 mesi fa. Per farlo dobbiamo spegnere i server e sperare che tutto si rialzi. Lo facciamo solo quando siamo davvero obbligati."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Patch OS/runtime ritardate per paura del downtime — finestra di vulnerabilità permanente | High | ECS: patching = nuovo Docker image; RDS: minor version upgrade automatica nella maintenance window; nessun accesso SSH ai server |

---

**Problema 5 — Osservabilità quasi nulla**

> *"Nagios ci dice se il server risponde al ping. Tutto il resto — latenza delle query, error rate dell'app, memoria heap, code batch — lo scopriamo quando ci chiamano gli utenti."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Nessun monitoring applicativo; nessuna visibilità su latenza, error rate, utilizzo risorse | High | CloudWatch Container Insights (ECS), RDS Performance Insights, CloudWatch Alarms su metriche applicative; dashboard unificata |

---

**Problema 6 — Ogni cambiamento è una bomba a orologeria**

> *"Le config le cambiamo direttamente in produzione via SSH. Non c'è staging, non c'è revisione, non c'è rollback. Un anno fa qualcuno ha fatto typo in `pg_hba.conf` e abbiamo passato 2 ore senza DB."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Config management manuale direttamente in produzione — nessun audit trail, nessun rollback | High | IaC Terraform + CI/CD pipeline: ogni cambio è un PR con review e apply automatico; SSM Parameter Store per config runtime |

---

**Problema 7 — Redis senza persistenza: un crash = tutti gli utenti sloggati**

> *"Redis non ha persistenza abilitata. Se crasha — e crasha, succede 2-3 volte l'anno — tutte le sessioni attive vengono perse. Gli utenti vengono buttati fuori mentre stanno lavorando. Poi ci chiamano arrabbiati."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Redis senza AOF persistence e senza replica — crash invalida tutte le sessioni attive | High | ElastiCache Redis con `appendonly yes` (AOF) + replication group Multi-AZ; failover automatico < 60s senza perdita sessioni |

---

**Problema 8 — Log retention di 7 giorni su disco locale**

> *"I log stanno sul disco del server. Li ruotiamo ogni 7 giorni. Se c'è un incidente di sicurezza e dobbiamo fare forensics, se lo scopriamo dopo una settimana non abbiamo più niente da guardare. Per il GDPR questo è un disastro."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Log retention 7 giorni su disco locale — forensics impossibile dopo incidenti; non conforme GDPR art. 32 | High | CloudWatch Logs con retention 90 giorni (hot) + S3 Glacier archivio 1 anno (freddo); log immutabili, non cancellabili dagli operatori |

---

**Problema 9 — Il batch job si blocca silenziosamente se il DB è irraggiungibile**

> *"Se c'è una partizione di rete tra il server batch e il DB, il job si blocca in attesa della connessione. Non ha timeout. L'abbiamo scoperto una mattina alle 8 quando il Finance team ci ha chiamato chiedendo i dati. Il job era fermo dalle 2 di notte."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Batch job senza connection timeout — si blocca silenziosamente su network partition per ore | High | `connect_timeout` nel `DATABASE_URL`; AWS Batch job timeout su EventBridge; CloudWatch alarm su execution time > 90 min |

---

**Problema 10 — Nessun ambiente di staging fedele alla produzione**

> *"Lo staging è un laptop di un developer che gira una versione diversa di Postgres. I bug che troviamo in produzione non si replicano mai lì. Ogni deploy in produzione è una sorpresa."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Nessuno staging environment fedele — bug produzione non riproducibili; ogni deploy è ad alto rischio | Medium | `docker compose` locale simula fedelmente il target cloud (MinIO = S3, Postgres = RDS, Redis = ElastiCache); stesso image in staging e prod via ECR |

---

> *"Ecco, questi sono i 10 problemi. Ma ce ne sono altri due che mi vengono in mente adesso..."*

**Bonus — Problema 11: nessun health check applicativo**

> *"ECS ha bisogno di sapere se il container è sano. Oggi non abbiamo nessun endpoint che risponda a questo. Se l'app è bloccata ma il processo è vivo, ECS non lo sa e non fa il restart."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Nessun `GET /healthz` endpoint — ECS non può distinguere tra container vivo e container bloccato | High | Aggiungere route `/healthz` che verifica connettività DB + Redis; ECS task replacement automatico su health check failure |

**Bonus — Problema 12: gestione dei secrets in chiaro nelle variabili d'ambiente del cron**

> *"Il crontab del server batch ha le credenziali scritte inline nel comando. Chiunque faccia `crontab -l` le vede. E vengono loggate da syslog."*

| Finding | Severity | Cloud fix |
|---------|----------|-----------|
| Credenziali in chiaro nel crontab di sistema — visibili in `crontab -l` e in syslog | Medium | AWS Batch non usa crontab; credenziali da Secrets Manager iniettate come env var cifrate; audit trail su ogni accesso al secret |

---

> *"Sai qual è la cosa più assurda? La metà di questi problemi li conosco da anni. Ma on-prem non hai alternative: o hai budget per raddoppiare l'hardware, oppure convivi con il rischio. In cloud diventa tutto un parametro di configurazione."*

---

## 10. Infrastructure as Code

| Finding | Severity | Fix |
|---------|----------|-----|
| No `.gitignore` entry for `*.tfstate` — state files risk being committed with credentials | Medium | Add `*.tfstate`, `*.tfstate.backup`, `.terraform/` to `.gitignore` |
| No remote state backend configured — concurrent Terraform runs will corrupt state | Medium | Configure S3 backend + DynamoDB lock table before first `terraform apply` |

---

## Pre-migration checklist (blockers before go-live)

The following **Critical** and **High** findings must be resolved before any workload goes live in production:

- [ ] Rotate `reporting_user` password; remove from all `.pgpass` files
- [ ] Remove `0.0.0.0/0` entry from `pg_hba.conf`
- [ ] Rotate core banking `dblink` credential; replace view with ETL pipeline
- [ ] Set `DEBUG = False` in web-app config
- [ ] Fix memory leak in session handler (do not migrate to ECS with active leak)
- [ ] Remove silent retry from batch job; implement fail-fast + CloudWatch alarm
- [ ] Enable `pgaudit` on RDS for GDPR compliance
- [ ] Pseudonymise customer data in CSV export views
- [ ] Rewrite `SECURITY DEFINER` stored procedures for RDS compatibility
- [ ] Block last 3 business days of each month from cutover activities

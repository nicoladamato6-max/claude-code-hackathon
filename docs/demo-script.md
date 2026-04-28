# Demo Script — Contoso Financial Cloud Migration
**Durata:** 12 minuti  
**Formato:** schermo condiviso, terminale + browser  
**Narrativa:** "Un'intervista ha trovato ciò che il code review non aveva trovato. Il codice lo risolve. I test lo provano."

---

## Preparazione (fai PRIMA della demo, non in diretta)

```bash
# 1. Verifica che Docker Desktop sia avviato

# 2. Avvia lo stack e aspetta che sia healthy
cd C:\Users\nicola.damato\Downloads\contoso-financial
docker compose up -d
docker compose ps
# Tutti i servizi devono mostrare "healthy" prima di procedere

# 3. Installa dipendenze test (una volta sola)
pip install -r tests/requirements.txt

# 4. Apri questi tab nel browser in anticipo:
#    Tab 1: http://localhost:5000          (web app)
#    Tab 2: presentation.html             (apri con browser: file:///...)
#    Tab 3: https://github.com/nicoladamato6-max/claude-code-hackathon/actions

# 5. Apri un terminale pulito nella cartella del progetto
#    Pulisci la history: clear

# 6. Tieni pronti questi file in un editor:
#    - docs/02-discovery.md   (aperto a §Intervista SRE)
#    - workloads/web-app/app.py  (aperto a riga _SECRET_KEYS)
```

---

## Parte 1 — Il problema (2 min)

**Apri:** `presentation.html` → slide 2 (The Challenge)

> "Contoso Financial aveva tre workload on-premise da migrare in cloud.
> Il CFO aveva firmato un contratto con scadenza fissa.
> Il CTO voleva cloud-native. La compliance richiedeva tutto in eu-west-1.
> Prima di scrivere una riga di codice, abbiamo fatto le interviste."

**Avanza a slide 3 (Discovery)**

> "Questa è la tecnica chiave: stakeholder role-play interviews.
> La domanda all'SRE — 'cosa non ti fa dormire la notte?' —
> ha surfacciato 12 problemi operativi invisibili all'analisi statica del codice."

**Mostra nel terminale:**

```bash
grep -A3 "cosa non ti fa dormire" docs/02-discovery.md
```

Output atteso:
```
> "Se brucia il datacenter, abbiamo perso tutto. Non abbiamo un sito DR."
> "La password del core banking è hardcoded nella view. È lì da 7 anni."
> "DEBUG = True. Non dovrebbe essere così ma non l'abbiamo mai cambiato."
```

> "Tre citazioni, tre vulnerabilità critiche. Nessuna emergeva dai log o dal codice."

---

## Parte 2 — Lo stack in esecuzione (2 min)

**Nel terminale:**

```bash
docker compose ps
```

> "Lo stesso docker-compose simula localmente tutti i servizi AWS:
> MinIO è il nostro S3, Postgres è RDS, Redis è ElastiCache."

**Apri il browser → Tab 1: http://localhost:5000/healthz**

> "L'endpoint /healthz controlla DB e Redis in real time."

Mostra il JSON di risposta (deve contenere `"status": "ok"`).

**Vai su http://localhost:5000** → fai login con:
- Username: `finance.user`
- Password: `changeme_local`

> "Login funzionante. Sessione Redis-backed con fallback automatico su filesystem
> se Redis è irraggiungibile — risolve il finding 'Redis crash = tutti gli utenti slogati'."

**Naviga su `/api/accounts`**

> "Ogni team vede solo i propri dati. Cinque ruoli read-only separati,
> uno per team. Risolve il finding 'shared reporting_user password'."

---

## Parte 3 — I finding risolti nel codice (2 min)

**Mostra nel terminale:**

```bash
grep -n "DEBUG\|SECRET_KEY\|SESSION_COOKIE" workloads/web-app/config.py
```

> "DEBUG defaults False. SECRET_KEY solleva eccezione se mancante —
> nessun fallback silenzioso. Cookie Secure e HttpOnly per default."

```bash
grep -n "_SECRET_KEYS\|_log" workloads/web-app/app.py | head -10
```

> "Il logger filtra automaticamente qualsiasi chiave che contiene
> password, secret, key, token, url. Le credenziali non entrano mai nei log."

```bash
grep -n "SECURITY INVOKER\|SECURITY DEFINER" workloads/reporting-db/migrations/V2__stored_procedures.sql | head -5
```

> "Le stored procedure erano SECURITY DEFINER — eseguivano come superuser.
> Reescritte tutte come SECURITY INVOKER. Senza questo, un utente con
> accesso limitato poteva escalare i privilegi."

---

## Parte 4 — I test lo provano (4 min)

**Nel terminale:**

```bash
pytest tests/smoke/ -v --tb=short 2>&1 | tail -30
```

> "22 smoke test. Connettività, versione PG15, pgaudit attivo, nessuna
> SECURITY DEFINER rimasta nel database. Se uno di questi fallisce,
> non si eseguono i layer superiori."

Aspetta il risultato (10-15 secondi). Deve mostrare `22 passed`.

```bash
pytest tests/contract/ -k "security" -v --tb=short 2>&1
```

> "I security test verificano le vulnerabilità per cui venivamo pagati.
> SQL injection nel campo username. Nel campo password. XSS reflection.
> Path traversal sulle asset key. User enumeration — il messaggio di errore
> è identico per utente sbagliato e password sbagliata."

Aspetta il risultato. Deve mostrare tutti `PASSED`.

```bash
pytest tests/batch/ -k "idempotency" -v --tb=short 2>&1
```

> "Il batch job ha un problema storico: usciva sempre con exit 0, anche in caso
> di errore. Ora esce con 1 su qualsiasi records_failed > 0.
> E se rilanciato per lo stesso giorno, trova il completed.marker su S3 e
> si ferma — nessun doppio processo, nessun dato sovrascritto."

---

## Parte 5 — I numeri (2 min)

**Avanza la presentazione → slide 12 (Summary)**

> "112 test su 4 layer. 36 finding scoperti con le interviste, 23 critici e high risolti nel codice.
> 11 ADR che documentano ogni scelta tecnologica con le alternative considerate.
> 8 documenti di engagement completi — memo, discovery, ADR, piano di migrazione,
> security review, compliance checklist, runbook, rollback plan.
> €723k di risparmio TCO in 3 anni. Breakeven in 7 mesi."

**Apri browser → Tab 3: GitHub Actions**

> "La CI pipeline gira su ogni push: test pyramid con fail-fast,
> terraform validate su tutti e 4 i moduli, Trivy per le CVE,
> Checkov per la security dell'IaC."

**Mostra il badge verde nel README su GitHub.**

> "Questo non è un prototipo. È pronto per andare in produzione."

---

## Domande frequenti — risposte pronte

**"Perché ECS e non EKS?"**
> "Il team non aveva esperienza Kubernetes in produzione. ECS ha un
> control plane gestito senza il costo fisso di €73/mese di EKS.
> Con 3 workload, non 300, Kubernetes è overengineering.
> La containerizzazione è preservata — in Phase 2 i task ECS diventano
> container Lambda senza ricostruire nulla."

**"Come gestite il rollback?"**
> "docs/08-rollback-plan.md: per ogni workload, per ogni stage,
> il rollback è un DNS switch. Massimo 10 minuti per web-app,
> 15 per reporting-db. RDS Multi-AZ ha RPO zero — failover automatico
> in meno di 60 secondi."

**"Come verificate il GDPR?"**
> "docs/06-compliance-checklist.md mappa 36 requisiti GDPR / EBA / EU AI Act
> al controllo specifico che li soddisfa e al test che lo verifica.
> 36 su 36. pgaudit cattura ogni query a livello database, CloudTrail
> ogni chiamata API. Tutto in eu-west-1, nessuna replica cross-region."

**"I test girano anche in CI?"**
> "Sì. .github/workflows/ci.yml: matrix sui 4 layer con fail-fast,
> terraform validate, ruff + mypy, Trivy CVE scan, Checkov IaC.
> Il badge nel README mostra lo stato dell'ultimo push."

---

## Piano B — se qualcosa non funziona

| Problema | Soluzione |
|---------|-----------|
| Docker non parte | Mostra `docker compose ps` dall'ultima run buona + screenshot |
| Test fallisce per rete | `pytest tests/batch/ -v` non richiede rete, solo il modulo Python |
| Browser non carica | Mostra il JSON grezzo con `curl http://localhost:5000/healthz` |
| GitHub Actions non verde | Mostra il log dell'ultima run andata bene dalla tab Actions |

---

## Ordine apertura schermi prima di iniziare

```
Terminale          →  cd contoso-financial  (pronto, history pulita)
Browser Tab 1      →  http://localhost:5000/healthz  (già caricato)
Browser Tab 2      →  presentation.html, slide 1  (già aperto)
Browser Tab 3      →  GitHub Actions, ultima run verde  (già aperto)
Editor Tab 1       →  docs/02-discovery.md
Editor Tab 2       →  workloads/web-app/app.py
```

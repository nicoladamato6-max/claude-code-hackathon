-- V1: Initial schema — all schemas, tables, and extensions
-- Reversible: V1__undo_init_schema.sql drops everything created here

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- crypt() for app.users passwords
CREATE EXTENSION IF NOT EXISTS pg_cron;    -- materialized view refresh (ADR-06)
CREATE EXTENSION IF NOT EXISTS pgaudit;    -- query-level audit logging (GDPR art. 32)

-- pgaudit: log all DDL and DML on reporting schemas
ALTER SYSTEM SET pgaudit.log = 'ddl, write, role';
ALTER SYSTEM SET pgaudit.log_relation = 'on';
SELECT pg_reload_conf();

-- ---------------------------------------------------------------------------
-- Schema layout (one schema per consumer team + app schema for web-app)
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS app;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS risk;
CREATE SCHEMA IF NOT EXISTS compliance;
CREATE SCHEMA IF NOT EXISTS ops;
CREATE SCHEMA IF NOT EXISTS exec;

-- ---------------------------------------------------------------------------
-- app schema — web-app users (5 internal teams)
-- ---------------------------------------------------------------------------
CREATE TABLE app.users (
    id            SERIAL PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,              -- bcrypt via pgcrypto crypt()
    team          TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    deactivated_at TIMESTAMPTZ              -- soft-delete; never DROP
);

-- Seed internal team accounts (password: changeme_local — rotate via Secrets Manager in cloud)
INSERT INTO app.users (username, password_hash, team) VALUES
    ('finance.user',    crypt('changeme_local', gen_salt('bf')), 'finance'),
    ('risk.user',       crypt('changeme_local', gen_salt('bf')), 'risk'),
    ('compliance.user', crypt('changeme_local', gen_salt('bf')), 'compliance'),
    ('ops.user',        crypt('changeme_local', gen_salt('bf')), 'ops'),
    ('exec.user',       crypt('changeme_local', gen_salt('bf')), 'exec')
ON CONFLICT (username) DO NOTHING;

-- ---------------------------------------------------------------------------
-- finance schema
-- ---------------------------------------------------------------------------
CREATE TABLE finance.accounts (
    account_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_name TEXT        NOT NULL,
    team         TEXT        NOT NULL,
    balance      NUMERIC(18,4) NOT NULL DEFAULT 0,
    currency     CHAR(3)     NOT NULL DEFAULT 'EUR',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE finance.transactions (
    transaction_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id       UUID        REFERENCES finance.accounts(account_id),
    transaction_date DATE        NOT NULL,
    amount           NUMERIC(18,4) NOT NULL,
    currency         CHAR(3)     NOT NULL DEFAULT 'EUR',
    status           TEXT        NOT NULL CHECK (status IN ('pending','settled','failed','cancelled')),
    description      TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON finance.transactions (transaction_date, status);

CREATE TABLE finance.reconciliation_log (
    id                   SERIAL PRIMARY KEY,
    transaction_id       UUID        REFERENCES finance.transactions(transaction_id),
    reconciliation_date  DATE        NOT NULL,
    reconciled_amount    NUMERIC(18,4),
    reconciled_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX ON finance.reconciliation_log (transaction_id, reconciliation_date);

-- Monthly close materialized view
CREATE MATERIALIZED VIEW finance.mv_monthly_close AS
SELECT
    date_trunc('month', t.transaction_date)::DATE AS month,
    a.team,
    a.currency,
    COUNT(*)                                       AS transaction_count,
    SUM(t.amount)                                  AS total_amount
FROM finance.transactions t
JOIN finance.accounts a ON a.account_id = t.account_id
WHERE t.status = 'settled'
GROUP BY 1, 2, 3
WITH NO DATA;

-- ---------------------------------------------------------------------------
-- risk schema
-- ---------------------------------------------------------------------------
CREATE TABLE risk.positions (
    position_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_code    TEXT        NOT NULL,
    quantity      NUMERIC(18,6) NOT NULL,
    book_value    NUMERIC(18,4) NOT NULL,
    market_value  NUMERIC(18,4),
    currency      CHAR(3)     NOT NULL DEFAULT 'EUR',
    position_date DATE        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON risk.positions (position_date);

CREATE MATERIALIZED VIEW risk.mv_var_daily AS
SELECT
    position_date,
    SUM(market_value - book_value) AS unrealised_pnl,
    PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY market_value - book_value) AS var_99
FROM risk.positions
WHERE market_value IS NOT NULL
GROUP BY position_date
WITH NO DATA;

-- ---------------------------------------------------------------------------
-- compliance schema
-- ---------------------------------------------------------------------------
CREATE TABLE compliance.audit_log (
    log_id      BIGSERIAL PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    actor       TEXT        NOT NULL,
    target_id   TEXT,
    details     JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ON compliance.audit_log (occurred_at DESC);

-- ---------------------------------------------------------------------------
-- ops schema
-- ---------------------------------------------------------------------------
CREATE TABLE ops.service_events (
    event_id    BIGSERIAL PRIMARY KEY,
    service     TEXT        NOT NULL,
    event_type  TEXT        NOT NULL,
    payload     JSONB,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- exec schema — executive summary without dblink (replaces v_executive_summary)
-- The dblink dependency (discovery §3) is replaced with a local join.
-- Core banking data is ingested nightly by the batch job into finance.transactions.
-- ---------------------------------------------------------------------------
CREATE VIEW exec.v_executive_summary AS
SELECT
    mc.month,
    mc.team,
    mc.currency,
    mc.transaction_count,
    mc.total_amount,
    vd.var_99
FROM finance.mv_monthly_close mc
LEFT JOIN risk.mv_var_daily vd
       ON vd.position_date = (mc.month + interval '1 month - 1 day')::DATE;

-- V3: Per-team read-only roles and pg_cron schedules
-- Replaces the shared reporting_user (discovery §4: never-rotated, all-schemas access).
-- Each consumer team gets a dedicated role with SELECT only on their schema.

-- ---------------------------------------------------------------------------
-- Per-team read-only roles (GDPR hardening — replaces shared reporting_user)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    roles TEXT[] := ARRAY['finance_ro', 'risk_ro', 'compliance_ro', 'ops_ro', 'exec_ro'];
    r TEXT;
BEGIN
    FOREACH r IN ARRAY roles LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END;
$$;

-- Grant SELECT per schema — each team only sees its own data
GRANT USAGE ON SCHEMA finance    TO finance_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA finance    TO finance_ro;

GRANT USAGE ON SCHEMA risk       TO risk_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA risk       TO risk_ro;

GRANT USAGE ON SCHEMA compliance TO compliance_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA compliance TO compliance_ro;

GRANT USAGE ON SCHEMA ops        TO ops_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA ops        TO ops_ro;

GRANT USAGE ON SCHEMA exec       TO exec_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA exec       TO exec_ro;

-- exec also needs to read from finance and risk to serve v_executive_summary
GRANT USAGE ON SCHEMA finance TO exec_ro;
GRANT USAGE ON SCHEMA risk    TO exec_ro;
GRANT SELECT ON finance.mv_monthly_close TO exec_ro;
GRANT SELECT ON risk.mv_var_daily        TO exec_ro;

-- Allow roles to call their own stored procedures (SECURITY INVOKER — caller needs perms)
GRANT EXECUTE ON FUNCTION finance.refresh_monthly_close() TO finance_ro;
GRANT EXECUTE ON FUNCTION risk.compute_var()              TO risk_ro;
GRANT EXECUTE ON FUNCTION exec.build_board_pack(DATE)     TO exec_ro;

-- ---------------------------------------------------------------------------
-- pg_cron: materialized view refresh schedules
-- Replaces the on-prem cron jobs on the DB host (discovery §6, SRE concern #3)
-- ---------------------------------------------------------------------------

-- Daily at 00:30 — Risk VaR must be ready before the 06:00 report
SELECT cron.schedule(
    'refresh-var-daily',
    '30 0 * * *',
    $$SELECT risk.compute_var()$$
);

-- 1st of each month at 01:00 — Finance monthly close
SELECT cron.schedule(
    'refresh-monthly-close',
    '0 1 1 * *',
    $$SELECT finance.refresh_monthly_close()$$
);

-- ---------------------------------------------------------------------------
-- Default privileges — future tables in each schema are accessible to team roles
-- ---------------------------------------------------------------------------
ALTER DEFAULT PRIVILEGES IN SCHEMA finance    GRANT SELECT ON TABLES TO finance_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA risk       GRANT SELECT ON TABLES TO risk_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA compliance GRANT SELECT ON TABLES TO compliance_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA ops        GRANT SELECT ON TABLES TO ops_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA exec       GRANT SELECT ON TABLES TO exec_ro;

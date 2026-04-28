-- V2: Stored procedures — SECURITY INVOKER replaces SECURITY DEFINER
-- Discovery §6: three procedures ran as superuser on on-prem PG13 via SECURITY DEFINER.
-- RDS does not grant rds_superuser to stored procedures. All three are rewritten to
-- SECURITY INVOKER: they execute with the caller's privileges, not the definer's.
-- Callers must have SELECT on the relevant tables (granted in V3).

-- ---------------------------------------------------------------------------
-- finance.refresh_monthly_close()
-- Called by pg_cron at 01:00 on the 1st of each month (configured in V3).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION finance.refresh_monthly_close()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY finance.mv_monthly_close;
END;
$$;

-- ---------------------------------------------------------------------------
-- risk.compute_var()
-- Called by pg_cron at 00:30 daily — must complete before Risk VaR report at 06:00.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION risk.compute_var()
RETURNS void
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY risk.mv_var_daily;
END;
$$;

-- ---------------------------------------------------------------------------
-- exec.build_board_pack()
-- Called manually or via EventBridge before board meetings.
-- Returns a JSON summary consumed by the Executive Reporting web endpoint.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION exec.build_board_pack(p_month DATE DEFAULT date_trunc('month', now())::DATE)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY INVOKER
AS $$
DECLARE
    result JSONB;
BEGIN
    SELECT jsonb_build_object(
        'month',             p_month,
        'generated_at',      now(),
        'total_transactions', SUM(transaction_count),
        'total_amount',      SUM(total_amount),
        'currencies',        jsonb_agg(DISTINCT currency),
        'var_99',            MAX(var_99)
    )
    INTO result
    FROM exec.v_executive_summary
    WHERE month = p_month;

    RETURN COALESCE(result, '{}'::JSONB);
END;
$$;

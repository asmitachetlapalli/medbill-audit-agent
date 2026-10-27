-- =====================================================================================
-- 02_analysis.sql — analysis of audit results.
--
-- Run after scripts.load_data and scripts.run_audits. Every query reads each bill's
-- LATEST audit run (the LATEST_AUDIT_* views), so re-running audits never double counts.
--
-- Each query starts with a "-- name: <export_name>" line. scripts/export_for_tableau.py
-- splits the file on those lines and writes each result to data/exports/<name>.csv.
-- You can also run any query directly in a Snowsight worksheet.
-- =====================================================================================

USE WAREHOUSE MEDBILL_WH;
USE SCHEMA MEDBILL_DB.AUDIT;


-- name: issue_type_summary
-- 1. Most common billing error types: how often each occurs, its share of all findings
--    (a window SUM over the grouped counts), the bills it touches, and dollars flagged.
WITH findings AS (
    SELECT ISSUE_TYPE, BILL_ID, AMOUNT_FLAGGED
    FROM LATEST_AUDIT_RESULTS
)
SELECT
    ISSUE_TYPE,
    COUNT(*)                                               AS FINDING_COUNT,
    ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1)       AS PCT_OF_FINDINGS,
    COUNT(DISTINCT BILL_ID)                                AS BILLS_AFFECTED,
    SUM(AMOUNT_FLAGGED)                                    AS DOLLARS_FLAGGED,
    ROUND(100 * SUM(AMOUNT_FLAGGED) / NULLIF(SUM(SUM(AMOUNT_FLAGGED)) OVER (), 0), 1)
                                                           AS PCT_OF_DOLLARS
FROM findings
GROUP BY ISSUE_TYPE
ORDER BY FINDING_COUNT DESC;


-- name: monthly_flagged_trend
-- 2. Total dollars flagged, by month of service, with a running (cumulative) total.
--    Bills with zero findings still count toward BILLS_AUDITED via the LEFT JOIN.
WITH audited_bills AS (
    SELECT b.BILL_ID, DATE_TRUNC('month', b.SERVICE_DATE) AS SERVICE_MONTH, b.TOTAL_CHARGED
    FROM LATEST_AUDIT_RUNS r
    JOIN BILLS b ON b.BILL_ID = r.BILL_ID
),
flagged_per_bill AS (
    SELECT BILL_ID, SUM(AMOUNT_FLAGGED) AS DOLLARS_FLAGGED
    FROM LATEST_AUDIT_RESULTS
    GROUP BY BILL_ID
),
monthly AS (
    SELECT
        a.SERVICE_MONTH,
        COUNT(*)                              AS BILLS_AUDITED,
        COUNT(f.BILL_ID)                      AS BILLS_WITH_ISSUES,
        SUM(a.TOTAL_CHARGED)                  AS TOTAL_BILLED,
        COALESCE(SUM(f.DOLLARS_FLAGGED), 0)   AS DOLLARS_FLAGGED
    FROM audited_bills a
    LEFT JOIN flagged_per_bill f ON f.BILL_ID = a.BILL_ID
    WHERE a.SERVICE_MONTH IS NOT NULL
    GROUP BY a.SERVICE_MONTH
)
SELECT
    SERVICE_MONTH,
    BILLS_AUDITED,
    BILLS_WITH_ISSUES,
    TOTAL_BILLED,
    DOLLARS_FLAGGED,
    SUM(DOLLARS_FLAGGED) OVER (ORDER BY SERVICE_MONTH
                               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                                                     AS CUMULATIVE_DOLLARS_FLAGGED
FROM monthly
ORDER BY SERVICE_MONTH;


-- name: billed_vs_cms_gap
-- 3. Billed price vs. the CMS Medicare benchmark, per procedure. MARKUP_VS_MEDICARE is
--    how many times the Medicare allowed amount the bills charged, per unit.
--    Empty until the CMS file is loaded.
WITH billed AS (
    SELECT
        CODE,
        COUNT(*)                                     AS LINE_COUNT,
        SUM(QUANTITY)                                AS UNITS_BILLED,
        SUM(CHARGE) / NULLIF(SUM(QUANTITY), 0)       AS AVG_BILLED_PER_UNIT
    FROM BILL_LINE_ITEMS
    GROUP BY CODE
)
SELECT
    b.CODE,
    COALESCE(ref.DESCRIPTION, cms.HCPCS_DESCRIPTION)         AS DESCRIPTION,
    ref.CATEGORY,
    b.LINE_COUNT,
    b.UNITS_BILLED,
    ROUND(b.AVG_BILLED_PER_UNIT, 2)                          AS AVG_BILLED_PER_UNIT,
    cms.AVG_SUBMITTED_CHARGE                                 AS CMS_AVG_SUBMITTED_CHARGE,
    cms.AVG_ALLOWED_AMOUNT                                   AS CMS_AVG_ALLOWED_AMOUNT,
    ROUND(b.AVG_BILLED_PER_UNIT - cms.AVG_ALLOWED_AMOUNT, 2) AS GAP_PER_UNIT,
    ROUND(b.AVG_BILLED_PER_UNIT / NULLIF(cms.AVG_ALLOWED_AMOUNT, 0), 2)
                                                             AS MARKUP_VS_MEDICARE
FROM billed b
JOIN CMS_REFERENCE_NATIONAL cms ON cms.HCPCS_CODE = b.CODE
LEFT JOIN CPT_REFERENCE ref     ON ref.CODE = b.CODE
ORDER BY MARKUP_VS_MEDICARE DESC NULLS LAST;


-- name: top_overcharged_procedures
-- 4. Procedures with the most overcharge dollars. RANK() gives the overall position;
--    a second RANK() partitioned by category gives the position within its category.
WITH overcharges AS (
    SELECT
        CODE,
        COUNT(*)                 AS OVERCHARGE_COUNT,
        COUNT(DISTINCT BILL_ID)  AS BILLS_AFFECTED,
        SUM(AMOUNT_FLAGGED)      AS DOLLARS_FLAGGED
    FROM LATEST_AUDIT_RESULTS
    WHERE ISSUE_TYPE = 'overcharge'
    GROUP BY CODE
),
ranked AS (
    SELECT
        o.*,
        COALESCE(ref.DESCRIPTION, 'Unknown')   AS DESCRIPTION,
        COALESCE(ref.CATEGORY, 'Other')        AS CATEGORY,
        RANK() OVER (ORDER BY o.DOLLARS_FLAGGED DESC)                    AS OVERALL_RANK,
        RANK() OVER (PARTITION BY COALESCE(ref.CATEGORY, 'Other')
                     ORDER BY o.DOLLARS_FLAGGED DESC)                    AS RANK_IN_CATEGORY
    FROM overcharges o
    LEFT JOIN CPT_REFERENCE ref ON ref.CODE = o.CODE
)
SELECT
    OVERALL_RANK, CODE, DESCRIPTION, CATEGORY, RANK_IN_CATEGORY,
    OVERCHARGE_COUNT, BILLS_AFFECTED, DOLLARS_FLAGGED
FROM ranked
WHERE OVERALL_RANK <= 15
ORDER BY OVERALL_RANK;


-- name: provider_error_rates
-- 5. Error rates by provider: share of bills with at least one finding, and share of
--    billed dollars flagged. Providers are ranked by the dollar share (highest risk = 1).
WITH bill_findings AS (
    SELECT
        r.BILL_ID,
        COUNT(f.RESULT_ID)                   AS FINDING_COUNT,
        COALESCE(SUM(f.AMOUNT_FLAGGED), 0)   AS DOLLARS_FLAGGED
    FROM LATEST_AUDIT_RUNS r
    LEFT JOIN LATEST_AUDIT_RESULTS f ON f.BILL_ID = r.BILL_ID
    GROUP BY r.BILL_ID
),
provider_stats AS (
    SELECT
        COALESCE(b.PROVIDER, 'Unknown')       AS PROVIDER,
        COALESCE(b.PROVIDER_TYPE, 'Unknown')  AS PROVIDER_TYPE,
        COUNT(*)                              AS BILLS_AUDITED,
        COUNT_IF(bf.FINDING_COUNT > 0)        AS BILLS_WITH_ISSUES,
        SUM(bf.FINDING_COUNT)                 AS FINDING_COUNT,
        SUM(b.TOTAL_CHARGED)                  AS TOTAL_BILLED,
        SUM(bf.DOLLARS_FLAGGED)               AS DOLLARS_FLAGGED
    FROM bill_findings bf
    JOIN BILLS b ON b.BILL_ID = bf.BILL_ID
    GROUP BY 1, 2
),
rates AS (
    SELECT
        *,
        ROUND(100 * BILLS_WITH_ISSUES / BILLS_AUDITED, 1)          AS PCT_BILLS_WITH_ISSUES,
        ROUND(100 * DOLLARS_FLAGGED / NULLIF(TOTAL_BILLED, 0), 1)  AS PCT_DOLLARS_FLAGGED
    FROM provider_stats
)
SELECT
    RANK() OVER (ORDER BY PCT_DOLLARS_FLAGGED DESC NULLS LAST)  AS RISK_RANK,
    PROVIDER, PROVIDER_TYPE, BILLS_AUDITED, BILLS_WITH_ISSUES, PCT_BILLS_WITH_ISSUES,
    FINDING_COUNT, TOTAL_BILLED, DOLLARS_FLAGGED, PCT_DOLLARS_FLAGGED
FROM rates
ORDER BY RISK_RANK;


-- name: detection_rate
-- 6. How well does the audit catch errors? Synthetic bills record the error planted on
--    each line (PLANTED_ERROR). A planted error counts as detected when the latest audit
--    of that bill raised the same issue type on that code (or, for unbundling, on either
--    code of the pair).
WITH planted AS (
    SELECT li.BILL_ID, li.CODE, li.PLANTED_ERROR
    FROM BILL_LINE_ITEMS li
    JOIN LATEST_AUDIT_RUNS r ON r.BILL_ID = li.BILL_ID
    WHERE li.PLANTED_ERROR IS NOT NULL
),
matched AS (
    SELECT
        p.BILL_ID,
        p.CODE,
        p.PLANTED_ERROR,
        MAX(IFF(f.RESULT_ID IS NOT NULL, 1, 0)) AS DETECTED
    FROM planted p
    LEFT JOIN LATEST_AUDIT_RESULTS f
      ON f.BILL_ID = p.BILL_ID
     AND f.ISSUE_TYPE = p.PLANTED_ERROR
     AND p.CODE IN (f.CODE, f.RELATED_CODE)
    GROUP BY p.BILL_ID, p.CODE, p.PLANTED_ERROR
)
SELECT
    PLANTED_ERROR                                  AS ERROR_TYPE,
    COUNT(*)                                       AS PLANTED,
    SUM(DETECTED)                                  AS DETECTED,
    ROUND(100 * SUM(DETECTED) / COUNT(*), 1)       AS DETECTION_RATE_PCT
FROM matched
GROUP BY PLANTED_ERROR
ORDER BY DETECTION_RATE_PCT, ERROR_TYPE;

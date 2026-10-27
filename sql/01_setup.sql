-- =====================================================================================
-- 01_setup.sql — one-time Snowflake setup for the MedBill Audit Agent.
--
-- Run this once in a Snowsight worksheet (as ACCOUNTADMIN on a trial account; select
-- "Run All"). It is safe to re-run: everything uses IF NOT EXISTS / OR REPLACE, and no
-- table is dropped, so loaded data and audit history survive.
-- =====================================================================================

-- -------------------------------------------------------------------------------------
-- Compute: the smallest warehouse, suspended after 60s idle so a free trial's credits
-- are only spent while a query is actually running. AUTO_RESUME wakes it on demand.
-- -------------------------------------------------------------------------------------
CREATE WAREHOUSE IF NOT EXISTS MEDBILL_WH
    WAREHOUSE_SIZE      = 'XSMALL'
    AUTO_SUSPEND        = 60
    AUTO_RESUME         = TRUE
    INITIALLY_SUSPENDED = TRUE
    COMMENT             = 'MedBill Audit Agent — loading, audits, and analysis';

-- Optional safety net: suspend the warehouse if it ever burns 10 credits in a month.
-- Requires ACCOUNTADMIN (the default role on a trial). Comment out if you lack it.
CREATE RESOURCE MONITOR IF NOT EXISTS MEDBILL_MONITOR
    WITH CREDIT_QUOTA = 10
         FREQUENCY = MONTHLY
         START_TIMESTAMP = IMMEDIATELY
    TRIGGERS ON 80 PERCENT DO NOTIFY
             ON 100 PERCENT DO SUSPEND;
ALTER WAREHOUSE MEDBILL_WH SET RESOURCE_MONITOR = MEDBILL_MONITOR;

-- -------------------------------------------------------------------------------------
-- Storage: one database, one schema for everything in this project.
-- -------------------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS MEDBILL_DB;
CREATE SCHEMA IF NOT EXISTS MEDBILL_DB.AUDIT;

USE WAREHOUSE MEDBILL_WH;
USE SCHEMA MEDBILL_DB.AUDIT;

-- -------------------------------------------------------------------------------------
-- BILLS: one row per bill (header). No patient names are stored.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS BILLS (
    BILL_ID          VARCHAR       NOT NULL PRIMARY KEY,  -- e.g. SYN-0001, PDF-sample
    SOURCE           VARCHAR       NOT NULL,              -- 'synthetic' or 'pdf'
    SOURCE_FILE      VARCHAR,
    PROVIDER         VARCHAR,
    PROVIDER_TYPE    VARCHAR,                             -- Hospital, Clinic, ...
    SERVICE_DATE     DATE,
    LINE_ITEM_COUNT  INTEGER,
    TOTAL_CHARGED    NUMBER(12, 2),
    LOADED_AT        TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- -------------------------------------------------------------------------------------
-- BILL_LINE_ITEMS: one row per billed service. CHARGE is the line total (all units).
-- PLANTED_ERROR is set only on synthetic bills: the error the generator deliberately
-- injected on that line, so we can measure how many the audit catches.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS BILL_LINE_ITEMS (
    BILL_ID        VARCHAR       NOT NULL REFERENCES BILLS (BILL_ID),
    LINE_NO        INTEGER       NOT NULL,
    CODE           VARCHAR       NOT NULL,                -- CPT or HCPCS code
    DESCRIPTION    VARCHAR,
    QUANTITY       INTEGER,
    CHARGE         NUMBER(12, 2),
    PLANTED_ERROR  VARCHAR,
    PRIMARY KEY (BILL_ID, LINE_NO)
);

-- -------------------------------------------------------------------------------------
-- CPT_REFERENCE: the curated reference set (data/cpt_codes.csv) — typical fair charge
-- and per-day unit limits that CMS data does not provide.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CPT_REFERENCE (
    CODE               VARCHAR NOT NULL PRIMARY KEY,
    DESCRIPTION        VARCHAR,
    CATEGORY           VARCHAR,
    MEDICARE_RATE      NUMBER(12, 2),
    TYPICAL_CHARGE     NUMBER(12, 2),
    MAX_UNITS_PER_DAY  INTEGER
);

-- -------------------------------------------------------------------------------------
-- CMS_REFERENCE_PRICING: national rows from CMS "Medicare Physician & Other
-- Practitioners — by Geography and Service". One row per code per place of service
-- (F = facility, O = office), exactly as published.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS CMS_REFERENCE_PRICING (
    HCPCS_CODE            VARCHAR NOT NULL,
    HCPCS_DESCRIPTION     VARCHAR,
    PLACE_OF_SERVICE      VARCHAR,
    TOTAL_PROVIDERS       INTEGER,
    TOTAL_SERVICES        NUMBER(18, 2),
    AVG_SUBMITTED_CHARGE  NUMBER(12, 2),   -- what providers billed Medicare, on average
    AVG_ALLOWED_AMOUNT    NUMBER(12, 2),   -- what Medicare allowed (the fair benchmark)
    AVG_PAYMENT_AMOUNT    NUMBER(12, 2),   -- what Medicare actually paid
    SOURCE_FILE           VARCHAR,
    LOADED_AT             TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

-- -------------------------------------------------------------------------------------
-- AUDIT_RUNS: one row per bill each time it is audited. Lets us find each bill's latest
-- audit (even one with zero findings) and gives error-rate denominators.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS AUDIT_RUNS (
    RUN_ID            VARCHAR       NOT NULL,
    BILL_ID           VARCHAR       NOT NULL REFERENCES BILLS (BILL_ID),
    OVERCHARGE_RATIO  NUMBER(4, 2),
    LINE_ITEM_COUNT   INTEGER,
    FINDING_COUNT     INTEGER,
    AUDITED_AT        TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP(),
    PRIMARY KEY (RUN_ID, BILL_ID)
);

-- -------------------------------------------------------------------------------------
-- AUDIT_RESULTS: one row per finding. AMOUNT_FLAGGED is the dollars in question for
-- that finding (see agent/audit.py); findings on the same line can overlap.
-- -------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS AUDIT_RESULTS (
    RESULT_ID       NUMBER        AUTOINCREMENT PRIMARY KEY,
    RUN_ID          VARCHAR       NOT NULL,
    BILL_ID         VARCHAR       NOT NULL REFERENCES BILLS (BILL_ID),
    CODE            VARCHAR,
    RELATED_CODE    VARCHAR,                             -- other code of an unbundled pair
    ISSUE_TYPE      VARCHAR       NOT NULL,              -- overcharge, duplicate, ...
    SEVERITY        VARCHAR,
    DESCRIPTION     VARCHAR,
    AMOUNT_FLAGGED  NUMBER(12, 2),
    CREATED_AT      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);
-- Note: Snowflake records PRIMARY KEY / REFERENCES constraints but does not enforce
-- them (except NOT NULL). They document the model; the loaders keep keys unique.

-- -------------------------------------------------------------------------------------
-- Views
-- -------------------------------------------------------------------------------------

-- One reference price per code: collapse facility/office rows into a service-volume-
-- weighted national average. The agent reads this view in Snowflake mode.
CREATE OR REPLACE VIEW CMS_REFERENCE_NATIONAL AS
SELECT
    HCPCS_CODE,
    MAX(HCPCS_DESCRIPTION)                                                    AS HCPCS_DESCRIPTION,
    SUM(TOTAL_SERVICES)                                                       AS TOTAL_SERVICES,
    ROUND(SUM(AVG_SUBMITTED_CHARGE * TOTAL_SERVICES) / NULLIF(SUM(TOTAL_SERVICES), 0), 2)
                                                                              AS AVG_SUBMITTED_CHARGE,
    ROUND(SUM(AVG_ALLOWED_AMOUNT * TOTAL_SERVICES) / NULLIF(SUM(TOTAL_SERVICES), 0), 2)
                                                                              AS AVG_ALLOWED_AMOUNT,
    ROUND(SUM(AVG_PAYMENT_AMOUNT * TOTAL_SERVICES) / NULLIF(SUM(TOTAL_SERVICES), 0), 2)
                                                                              AS AVG_PAYMENT_AMOUNT
FROM CMS_REFERENCE_PRICING
GROUP BY HCPCS_CODE;

-- Each bill's most recent audit run. QUALIFY filters on a window function directly,
-- without a wrapping subquery.
CREATE OR REPLACE VIEW LATEST_AUDIT_RUNS AS
SELECT *
FROM AUDIT_RUNS
QUALIFY ROW_NUMBER() OVER (PARTITION BY BILL_ID ORDER BY AUDITED_AT DESC) = 1;

-- Findings from each bill's latest run only, so re-auditing never double counts.
CREATE OR REPLACE VIEW LATEST_AUDIT_RESULTS AS
SELECT res.*
FROM AUDIT_RESULTS res
JOIN LATEST_AUDIT_RUNS run
  ON run.RUN_ID = res.RUN_ID
 AND run.BILL_ID = res.BILL_ID;

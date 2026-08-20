# CoCoEV ProcureIQ — Streamlit Web Interface

Procurement Intelligence Platform for CoCoEV electric scooter manufacturing.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Streamlit (Local / SiS)                       │
│                                                                  │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────────┐ │
│  │ Agentic Control  │  │ Procurement      │  │ Talk to Your  │ │
│  │ Tower (main)     │  │ Dashboard        │  │ Data          │ │
│  └────────┬─────────┘  └────────┬─────────┘  └──────┬────────┘ │
│           │                     │                     │          │
│           └─────────────────────┼─────────────────────┘          │
│                                 │                                 │
│                    snowflake-connector-python                     │
└─────────────────────────────────┼────────────────────────────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │         Snowflake          │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ GOLD Schema          │  │
                    │  │ (Star Schema)        │  │
                    │  │ 7 DIM + 6 FCT tables │  │
                    │  └──────────────────────┘  │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ ACTION Schema        │  │
                    │  │ (Cases, Audit, Events │  │
                    │  │  Investigations)      │  │
                    │  └──────────────────────┘  │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ Semantic View +       │  │
                    │  │ Cortex Agent          │  │
                    │  └──────────────────────┘  │
                    └────────────────────────────┘
```

## Pages

| Page | File | Purpose |
|------|------|---------|
| Agentic Control Tower | `Agentic_Control_Tower.py` | Case lifecycle, 5-phase AI pipeline, persona tabs, action buttons |
| Procurement Dashboard | `pages/2_Dashboard.py` | Spend trends, KPIs, gap analysis, AI insights per chart |
| Talk to Your Data | `pages/3_Ask_Data.py` | Natural language Q&A via Cortex Agent (DATA_AGENT_RUN) |
| How It Works | `pages/4_How_It_Works.py` | Architecture explainer with diagrams |

---

## Data Schema Requirements

### GOLD Schema (read-only)

| Table | Key Numeric Columns | Notes |
|-------|-------------------|-------|
| `FCT_PURCHASE_ORDERS` | NET_VALUE, GROSS_VALUE, QUANTITY, NET_PRICE (all NUMBER), FISCAL_YEAR (NUMBER), VENDOR_SK, MATERIAL_SK, PLANT_SK, DATE_KEY (all NUMBER) | PO_ID and PO_LINE are VARCHAR |
| `FCT_AP_INVOICES` | GROSS_INVOICE_AMOUNT (NUMBER), VENDOR_SK, DATE_KEY, COMPANY_CODE_SK (NUMBER) | FISCAL_YEAR is VARCHAR |
| `FCT_PO_HISTORY` | QUANTITY, AMOUNT_LOCAL_CURRENCY, AMOUNT_DOCUMENT_CURRENCY (all NUMBER), DATE_KEY, PLANT_SK, MATERIAL_SK (NUMBER) | FISCAL_YEAR is VARCHAR |
| `FCT_GOODS_MOVEMENTS` | QUANTITY, AMOUNT_LOCAL_CURRENCY (NUMBER), DATE_KEY, MATERIAL_SK, PLANT_SK (NUMBER) | FISCAL_YEAR is VARCHAR |
| `FCT_GL_TRANSACTIONS` | AMOUNT_LOCAL_CURRENCY, AMOUNT_DOCUMENT_CURRENCY (NUMBER), DATE_KEY, COMPANY_CODE_SK, VENDOR_SK (NUMBER) | FISCAL_YEAR is VARCHAR |
| `FCT_AP_OPEN_ITEMS` | AMOUNT_LOCAL_CURRENCY, AMOUNT_DOCUMENT_CURRENCY (NUMBER), DUE_DATE_KEY, COMPANY_CODE_SK, VENDOR_SK (NUMBER) | FISCAL_YEAR is VARCHAR |
| `DIM_VENDOR` | VENDOR_SK (NUMBER) | |
| `DIM_MATERIAL` | MATERIAL_SK (NUMBER) | |
| `DIM_PLANT` | PLANT_SK (NUMBER) | |
| `DIM_DATE` | DATE_KEY, YEAR, QUARTER, MONTH (all NUMBER) | FULL_DATE is VARCHAR |
| `DIM_COMPANY_CODE` | COMPANY_CODE_SK (NUMBER) | |
| `DIM_VENDOR_COMPANY` | VENDOR_COMPANY_SK (NUMBER) | |
| `DIM_STORAGE_LOCATION` | STORAGE_LOCATION_SK (NUMBER) | |

### ACTION Schema (read-write)

| Table | Key Numeric Columns | Key Timestamp Columns | Notes |
|-------|-------------------|---------------------|-------|
| `AI_PROCUREMENT_CASE` | FINANCIAL_IMPACT (NUMBER), RISK_SCORE (NUMBER), EVENT_ID (NUMBER), INVESTIGATION_ID (NUMBER) | CREATED_AT (TIMESTAMP_NTZ), UPDATED_AT (TIMESTAMP_NTZ) | **Critical: FINANCIAL_IMPACT must be NUMBER for SUM/ORDER BY** |
| `AI_AUDIT_LOG` | — | CREATED_AT (TIMESTAMP_NTZ) | **Critical: must be valid timestamps, not text** |
| `BUSINESS_EVENT` | EVENT_ID (NUMBER), IMPACT_USD (NUMBER), IMPACT_LOCAL (NUMBER) | DETECTED_AT, FIRST_SEEN_AT, LAST_SEEN_AT (TIMESTAMP_NTZ) | **Critical: IMPACT_USD must be NUMBER for ABS/ORDER BY** |
| `INVESTIGATION` | INVESTIGATION_ID (NUMBER), EVENT_ID (NUMBER), CONFIDENCE (NUMBER) | INVESTIGATED_AT (TIMESTAMP_NTZ) | |
| `RISK_ASSESSMENT` | RISK_ID (NUMBER), EVENT_ID (NUMBER), COMPOSITE_SCORE (NUMBER) | ASSESSED_AT (TIMESTAMP_NTZ) | |
| `ACTION_PLAN` | — | PLANNED_AT (TIMESTAMP_NTZ) | |
| `ACTION_LOG` | — | CREATED_AT (TIMESTAMP_NTZ) | |

### Semantic View

- `GOLD.SV_PROCURE_TO_PAY_FINANCE` — Required for "Talk to Your Data" page (Cortex Agent)

---

## Known Data Type Issues (Backup/Restore)

**IMPORTANT**: When restoring from parquet backups (`restore_from_parquet.py`), the `write_pandas` function infers column types from the parquet file. This can cause:

1. **Numeric columns stored as VARCHAR** — Columns like `FINANCIAL_IMPACT`, `IMPACT_USD`, `NET_VALUE` may be written as TEXT. The Python connector then fails with:
   ```
   252005: Failed to convert current row, cause: [Errno 84] Value too large to be stored in data type
   ```
   This happens when SQL operations (SUM, ABS, ORDER BY DESC) produce numeric results that the connector tries to fit into a Python string.

2. **Timestamp columns with invalid values** — If timestamps were serialized as strings in parquet (e.g., JavaScript `"Invalid Date"`), they get stored literally in TIMESTAMP_NTZ columns. The Python connector crashes when trying to parse these as datetime objects.

### How to Fix After Restore

Run this pattern for each affected column:

```sql
-- Fix VARCHAR → NUMBER
ALTER TABLE <table> ADD COLUMN <col>_FIXED NUMBER(38,2);
UPDATE <table> SET <col>_FIXED = TRY_TO_NUMBER(<col>, 38, 2);
ALTER TABLE <table> DROP COLUMN <col>;
ALTER TABLE <table> RENAME COLUMN <col>_FIXED TO <col>;

-- Fix invalid timestamps
UPDATE <table> SET <col> = '2026-08-18 05:50:00'::TIMESTAMP_NTZ
WHERE TO_VARCHAR(<col>) LIKE '%Invalid%' OR <col> IS NULL;
```

### Validation Query

Run this after any restore to detect problems:

```sql
-- Check for TEXT columns that should be NUMBER
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE
FROM SAP_P2P_FINANCE_DEV.INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA IN ('GOLD', 'ACTION')
  AND COLUMN_NAME IN (
    'NET_VALUE','GROSS_VALUE','QUANTITY','NET_PRICE','GROSS_INVOICE_AMOUNT',
    'AMOUNT_LOCAL_CURRENCY','AMOUNT_DOCUMENT_CURRENCY',
    'FINANCIAL_IMPACT','RISK_SCORE','IMPACT_USD','IMPACT_LOCAL',
    'CONFIDENCE','COMPOSITE_SCORE','EVENT_ID','INVESTIGATION_ID',
    'VENDOR_SK','MATERIAL_SK','PLANT_SK','DATE_KEY','DUE_DATE_KEY',
    'COMPANY_CODE_SK','CASH_DISCOUNT_DAYS_1'
  )
  AND DATA_TYPE = 'TEXT'
ORDER BY TABLE_SCHEMA, TABLE_NAME;

-- Check for invalid timestamps
SELECT 'AI_PROCUREMENT_CASE' AS TBL, COUNT(*) AS BAD
FROM SAP_P2P_FINANCE_DEV.ACTION.AI_PROCUREMENT_CASE
WHERE TO_VARCHAR(CREATED_AT) LIKE '%Invalid%'
UNION ALL
SELECT 'AI_AUDIT_LOG', COUNT(*)
FROM SAP_P2P_FINANCE_DEV.ACTION.AI_AUDIT_LOG
WHERE TO_VARCHAR(CREATED_AT) LIKE '%Invalid%';
```

---

## Deployment

### Streamlit-in-Snowflake (SiS)

```bash
cd web-interface
snow streamlit deploy --connection <connection-name> \
  --database SAP_P2P_FINANCE_DEV --schema ACTION --replace
```

### Local Development

```bash
cd web-interface
pip install -r requirements.txt
streamlit run Agentic_Control_Tower.py
```

Create `.streamlit/secrets.toml` with:
```toml
SNOWFLAKE_ACCOUNT = "your-account-identifier"
SNOWFLAKE_USER = "your-user"
SNOWFLAKE_PASSWORD = "your-password"
SNOWFLAKE_WAREHOUSE = "COMPUTE_WH"
SNOWFLAKE_DATABASE = "SAP_P2P_FINANCE_DEV"
SNOWFLAKE_SCHEMA = "GOLD"
SNOWFLAKE_ROLE = "ACCOUNTADMIN"
```

**Never commit `secrets.toml` to git** (it's in `.gitignore`).

### Restore to a New Account

```bash
# 1. Restore data from parquet
python gold_sql/restore_from_parquet.py

# 2. Validate column types (run the validation query above)

# 3. Fix any TEXT→NUMBER issues found

# 4. Create semantic view (see gold_sql/ for DDL)

# 5. Deploy Streamlit
snow streamlit deploy --connection <target> --database SAP_P2P_FINANCE_DEV --schema ACTION --replace
```

---

## Control Tower UI Specification

### Layout (top to bottom)

1. **Header** — Title + caption + flow steps banner
   ```
   Detect → Investigate → Risk Score/Decide → Plan Actions → Create Cases/Audit
   Every decision is captured in the audit trail.
   ```

2. **KPI Row** (6 metrics in bordered cards)
   - Total Spend (FY25) — from `FCT_AP_INVOICES`
   - Invoices — count from `FCT_AP_INVOICES`
   - Purchase Orders — distinct PO count from `FCT_PURCHASE_ORDERS`
   - Financial Exposure — SUM of `FINANCIAL_IMPACT` from `AI_PROCUREMENT_CASE`
   - Open Risk Cases — count where STATUS in open states
   - Actions Pending — count where STATUS = 'AWAITING_DECISION'

3. **Priority Findings** — Tabbed by persona (Procurement Manager, Finance Manager, etc.)
   - Each tab shows cases assigned to that persona with severity badge, vendor, impact, status
   - "⚡ Take Action" button (teal gradient) to select a case

4. **🚀 Run AI Investigation** button (purple/blue gradient)
   - Triggers 5-phase pipeline: Detection → Investigation → Risk Score → Action Plan → Case Creation
   - Shows real-time progress via `st.status()`

5. **Case Detail** (when a case is selected)
   - Case header with severity badge, status, exposure amount
   - Five-Why Analysis (expandable)
   - Ranked Hypotheses (expandable)
   - AI Recommendation
   - Action buttons: Execute Payment Hold, Create Investigation Task, Resolve/Dismiss
   - Audit Trail (expandable)

6. **Export HTML Report** button

### Button Color Scheme

| Button | Style | Meaning |
|--------|-------|---------|
| 🚀 Run AI Investigation | Purple → Blue gradient (`#7c3aed → #2563eb`) | AI/Intelligence action |
| ⚡ Take Action | Teal/Emerald gradient (`#059669 → #0d9488`) | Operational action |
| Execute Payment Hold | Primary (default Streamlit blue) | Critical financial action |
| Create Investigation Task / Resolve | Secondary (outline) | Standard workflow actions |

---

## Requirements

```
streamlit>=1.32.0
snowflake-connector-python[pandas]>=3.6.0
altair>=5.0.0
pandas>=2.0.0
```

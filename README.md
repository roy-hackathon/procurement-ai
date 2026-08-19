# CoCoEV ProcureIQ — Streamlit Web Interface

Procurement Intelligence Platform for CoCoEV electric scooter manufacturing.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Streamlit Cloud                            │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────┐  ┌───────────┐ │
│  │ Procurement      │  │ AI Event         │  │ AI Agent  │ │
│  │ Dashboard        │  │ Detector         │  │ Reasoning │ │
│  │ (page 1)        │  │ (page 2)         │  │ (page 3)  │ │
│  └────────┬─────────┘  └────────┬─────────┘  └─────┬─────┘ │
│           │                     │                    │       │
│           └─────────────────────┼────────────────────┘       │
│                                 │                            │
│                    snowflake-connector-python                 │
└─────────────────────────────────┼────────────────────────────┘
                                  │
                    ┌─────────────┼─────────────┐
                    │         Snowflake          │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ GOLD Schema          │  │
                    │  │ (Star Schema)        │  │
                    │  │ 8 DIM + 9 FCT tables │  │
                    │  └──────────────────────┘  │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ ACTION Schema        │  │
                    │  │ (Detector views,     │  │
                    │  │  audit state)        │  │
                    │  └──────────────────────┘  │
                    │                            │
                    │  ┌──────────────────────┐  │
                    │  │ Cortex Agent         │  │
                    │  │ (LLM reasoning,      │  │
                    │  │  Five-Why analysis)  │  │
                    │  └──────────────────────┘  │
                    └────────────────────────────┘
```

## Why Cortex Agent? (Design Decision)

The `ai-business-event-detector` skill was originally designed to run inside Cortex Code (CoCo),
where the LLM agent reasons over SQL results in a multi-turn loop. For a Streamlit Cloud
deployment (no local CoCo CLI), we evaluated these options:

| Approach | LLM Reasoning? | Local CoCo Required? | Works on Streamlit Cloud? |
|----------|---------------|---------------------|--------------------------|
| Cortex Code Agent SDK | Yes | Yes (CLI on PATH) | No |
| Deterministic scripts (`run_daily.py`) | No (rule-based only) | No | Yes |
| **Snowflake Cortex Agent** | **Yes** | **No (server-side)** | **Yes** |
| Standard Cortex AI functions | Partial (single call) | No | Yes |

**Selected: Snowflake Cortex Agent** because:
1. Runs entirely server-side in Snowflake — no local tooling needed
2. Has LLM reasoning (Claude Sonnet 4.5) for Five-Why investigation
3. Can execute code/SQL against the GOLD and ACTION schemas
4. Callable from Streamlit via `SNOWFLAKE.CORTEX.DATA_AGENT_RUN()` SQL function
5. Maintains the skill's reasoning quality without requiring CoCo CLI

The agent (`SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT`) has the full skill
instructions embedded as its system prompt, with access to the code_execution tool
for querying Snowflake tables.

## Pages

| Page | Purpose |
|------|---------|
| `app.py` | Home page |
| `pages/1_Procurement_Dashboard.py` | KPI cards, spend trends, vendor/region/pipeline charts with filters |
| `pages/2_AI_Event_Detector.py` | Run detection scans, view business events from ACTION schema |
| `pages/3_AI_Reasoning_Agent.py` | Interactive Cortex Agent — Five-Why investigation, risk assessment |

## Deployment to Streamlit Community Cloud

### Prerequisites
- GitHub repository with this code pushed
- Snowflake account with SAP_P2P_FINANCE_DEV database deployed

### Steps

1. **Push to GitHub**
   ```bash
   git add web-interface/
   git commit -m "Add Streamlit procurement dashboard"
   git push
   ```

2. **Go to [share.streamlit.io](https://share.streamlit.io)** and sign in with GitHub.

3. **Create new app:**
   - Repository: your GitHub repo
   - Branch: `main`
   - Main file path: `web-interface/app.py`

4. **Add secrets** (Advanced Settings > Secrets):
   ```toml
   SNOWFLAKE_ACCOUNT = "your-account-identifier"
   SNOWFLAKE_USER = "your-user"
   SNOWFLAKE_PASSWORD = "your-password"
   SNOWFLAKE_WAREHOUSE = "COMPUTE_WH"
   SNOWFLAKE_DATABASE = "SAP_P2P_FINANCE_DEV"
   SNOWFLAKE_SCHEMA = "GOLD"
   SNOWFLAKE_ROLE = "ACCOUNTADMIN"
   ```

5. **Deploy** — the app will install dependencies from `requirements.txt`.

### Important Notes
- Never commit `secrets.toml` to git (it's in `.gitignore`)
- The Cortex Agent runs server-side in Snowflake — no additional setup needed
- Detection page writes to ACTION schema — ensure the role has INSERT/UPDATE privileges

## Local Development

```bash
cd web-interface
pip install -r requirements.txt
streamlit run app.py
```

Create `.streamlit/secrets.toml` with your Snowflake credentials (same format as above).

## Snowflake Objects Required

### Gold Layer (read-only for dashboard)
- `SAP_P2P_FINANCE_DEV.GOLD.*` — 8 dimension + 9 fact tables

### Action Layer (read-write for detector)
- `SAP_P2P_FINANCE_DEV.ACTION.DETECTOR_REGISTRY` — registered detector view definitions
- `SAP_P2P_FINANCE_DEV.ACTION.BUSINESS_EVENT` — detected anomalies
- `SAP_P2P_FINANCE_DEV.ACTION.VW_DETECT_*` — 6 detector views

### Cortex Agent (for reasoning page)
- `SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT` — Cortex Agent object

### Rebuild from Parquet (if needed)
```bash
python gold_sql/rebuild_gold_from_parquet.py --connection <your-connection>
```

## Requirements
```
streamlit>=1.32.0
snowflake-connector-python[pandas]>=3.6.0
plotly>=5.18.0
pandas>=2.0.0
```

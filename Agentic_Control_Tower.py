import streamlit as st
import pandas as pd
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import get_snowflake_connection, is_connected, run_query_df, run_query
from services.pipeline import run_full_pipeline_with_cases, next_run_id, reset_action_schema, DATABASE
from services.case_manager import (
    get_cases, get_case, update_case_status, audit_log,
    get_audit_trail, execute_action, get_kpi_summary
)
from components.persona import persona_selector

st.set_page_config(
    page_title="CoCoEV Procurement Control Tower",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Procurement Control Tower")
st.caption("Powered by 5-Step Multi-Agent Orchestration")
persona = "procurement_manager"  # default persona for actions

st.markdown("""
<div style="background:#f8fafc;border-left:4px solid #2563eb;padding:12px 16px;margin-bottom:20px;border-radius:4px;font-size:14px;">
<b>Detect → Investigate → Risk Score/Decide → Plan Actions → Create Cases/Audit</b><br>
<span style="font-size:12px;color:#6b7280;">Every decision is captured in the audit trail.</span><br><br>
This Control Tower monitors CoCoEV's procurement risks across 3 plants and 10 suppliers.
AI agents detect anomalies, investigate root causes with Five-Why reasoning, recommend actions,
and enable one-click execution — all with full audit trail for governance.
</div>
""", unsafe_allow_html=True)

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

conn = get_snowflake_connection()

# --- Sidebar: Account Info ---
from components.sidebar_info import render_account_info
render_account_info()

# Card border styling + colored metrics for risk/action cards
st.markdown("""
<style>
[data-testid="stMetric"] {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
/* Take Action buttons - teal/emerald gradient */
button[data-testid="stBaseButton-secondary"] {
    background: linear-gradient(135deg, #059669, #0d9488) !important;
    color: white !important;
    border: none !important;
    font-weight: 600;
    border-radius: 8px;
}
</style>
""", unsafe_allow_html=True)


# ============================================================
# HELPER: REPORT GENERATION (must be defined before use)
# ============================================================

def _generate_case_report(case):
    """Generate HTML report for a single case."""
    inv_data = []
    if case.get("INVESTIGATION_ID"):
        inv_data = run_query(f"""
            SELECT NARRATIVE, HYPOTHESES, CONFIDENCE, ROOT_CAUSE_BRANCH
            FROM {DATABASE}.ACTION.INVESTIGATION
            WHERE INVESTIGATION_ID = {case['INVESTIGATION_ID']}
        """)

    audit_data = run_query(f"""
        SELECT EVENT_TYPE, ACTOR_TYPE, ACTOR, DESCRIPTION, CREATED_AT
        FROM {DATABASE}.ACTION.AI_AUDIT_LOG
        WHERE CASE_ID = '{case["CASE_ID"]}'
        ORDER BY CREATED_AT
    """)

    narrative_html = ""
    hyp_html = ""
    if inv_data:
        inv = inv_data[0]
        for line in (inv.get("NARRATIVE") or "").split("\n"):
            if line.strip():
                narrative_html += f"<li>{line.strip()}</li>"
        hyps = inv.get("HYPOTHESES")
        if hyps:
            try:
                hyp_list = json.loads(hyps) if isinstance(hyps, str) else hyps
                if isinstance(hyp_list, list):
                    for h in hyp_list:
                        score = float(h.get("score", 0)) * 100
                        hyp_html += f"<tr><td>{h.get('branch','')}</td><td>{score:.0f}%</td><td>{h.get('reason','')}</td></tr>"
                else:
                    hyp_html += f"<tr><td colspan='3'>{str(hyps)}</td></tr>"
            except (json.JSONDecodeError, TypeError):
                for line in str(hyps).split("\n"):
                    if line.strip():
                        hyp_html += f"<tr><td colspan='3'>{line.strip()}</td></tr>"

    audit_html = ""
    for a in audit_data:
        audit_html += f"<tr><td>{str(a.get('CREATED_AT',''))[:16]}</td><td>{a.get('ACTOR','')}</td><td>{a.get('DESCRIPTION','')}</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Case Report: {case['CASE_ID']}</title>
<style>
body{{font-family:system-ui;max-width:900px;margin:40px auto;padding:0 20px;color:#1a1a1a}}
h1{{color:#1e40af}} h2{{color:#374151;border-bottom:1px solid #e5e7eb;padding-bottom:8px}}
table{{width:100%;border-collapse:collapse;margin:16px 0}}
th,td{{padding:8px 12px;border:1px solid #e5e7eb;text-align:left;font-size:13px}}
th{{background:#f9fafb;font-weight:600}}
.badge{{display:inline-block;padding:4px 8px;border-radius:4px;font-size:11px;font-weight:600;color:white}}
.critical{{background:#dc2626}} .high{{background:#ea580c}} .medium{{background:#2563eb}}
ol{{line-height:1.8}}
</style></head><body>
<h1>Procurement Investigation Report</h1>
<p><b>Case:</b> {case['CASE_ID']} | <b>Generated:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>

<h2>Executive Summary</h2>
<table>
<tr><th>Supplier</th><td>{case.get('VENDOR_NAME','')}</td><th>Risk Level</th><td><span class="badge {(case.get('RISK_LEVEL') or '').lower()}">{case.get('RISK_LEVEL','')}</span></td></tr>
<tr><th>Issue</th><td>{case.get('CASE_TYPE','')}</td><th>Financial Impact</th><td>${float(case.get('FINANCIAL_IMPACT') or 0):,.0f}</td></tr>
<tr><th>Root Cause</th><td>{case.get('ROOT_CAUSE','')}</td><th>Status</th><td>{case.get('STATUS','')}</td></tr>
<tr><th>Risk Score</th><td>{case.get('RISK_SCORE','')}</td><th>Owner</th><td>{case.get('OWNER','')}</td></tr>
</table>

<h2>Five-Why Analysis</h2>
<ol>{narrative_html if narrative_html else '<li>Not yet investigated</li>'}</ol>

<h2>Ranked Hypotheses</h2>
<table><tr><th>Branch</th><th>Confidence</th><th>Reasoning</th></tr>
{hyp_html if hyp_html else '<tr><td colspan="3">No hypotheses generated</td></tr>'}
</table>

<h2>AI Recommendation</h2>
<p>{case.get('RECOMMENDATION','No recommendation yet')}</p>

<h2>Audit Trail</h2>
<table><tr><th>Timestamp</th><th>Actor</th><th>Event</th></tr>
{audit_html if audit_html else '<tr><td colspan="3">No audit events</td></tr>'}
</table>

<hr><p style="color:#666;font-size:11px;">ProcureAI — Powered by 5-Step Multi-Agent Orchestration</p>
</body></html>"""

# ============================================================
# SECTION 1: EXECUTIVE KPIs
# ============================================================

kpi_data = get_kpi_summary()

# Also get GOLD-layer procurement KPIs
gold_kpis = run_query(f"""
    SELECT
        COALESCE(SUM(GROSS_INVOICE_AMOUNT), 0) AS TOTAL_SPEND,
        COUNT(*) AS INVOICE_COUNT,
        COUNT(DISTINCT VENDOR_SK) AS VENDOR_COUNT
    FROM {DATABASE}.GOLD.FCT_AP_INVOICES
    WHERE FISCAL_YEAR = '2025'
""")
po_kpis = run_query(f"""
    SELECT COUNT(DISTINCT PO_ID) AS PO_COUNT
    FROM {DATABASE}.GOLD.FCT_PURCHASE_ORDERS
    WHERE FISCAL_YEAR = '2025'
""")

gold = gold_kpis[0] if gold_kpis else {}
po = po_kpis[0] if po_kpis else {}

total_spend = float(gold.get("TOTAL_SPEND") or 0)
total_exposure = float(kpi_data.get("TOTAL_EXPOSURE") or 0)
open_cases = int(kpi_data.get("OPEN_CASES") or 0)
actions_pending = int(kpi_data.get("ACTIONS_PENDING") or 0)

kpi_cols = st.columns(6)
with kpi_cols[0]:
    spend_str = f"${total_spend/1e6:.0f}M" if total_spend >= 1e6 else f"${total_spend:,.0f}"
    st.metric("Total Spend (FY25)", spend_str)
    st.caption("AP invoice spend across all vendors and plants for the fiscal year.")
with kpi_cols[1]:
    st.metric("Invoices", f"{int(gold.get('INVOICE_COUNT') or 0):,}")
    st.caption("Vendor invoices received and processed through the P2P cycle.")
with kpi_cols[2]:
    st.metric("Purchase Orders", f"{int(po.get('PO_COUNT') or 0):,}")
    st.caption("Distinct PO documents raised by procurement across all categories.")
with kpi_cols[3]:
    exp_str = f"${total_exposure/1e6:.0f}M"
    st.markdown(f"""
    <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:14px 16px;">
        <div style="font-size:0.875rem;color:#6b7280;font-weight:400;">Financial Exposure</div>
        <div style="font-size:1.75rem;font-weight:700;color:#dc2626;">{exp_str}</div>
    </div>
    """, unsafe_allow_html=True)
    st.caption("Total at-risk amount from AI-detected anomalies requiring action.")
with kpi_cols[4]:
    st.markdown(f"""
    <div style="background:#fff7ed;border:1px solid #fdba74;border-radius:8px;padding:14px 16px;">
        <div style="font-size:0.875rem;color:#6b7280;font-weight:400;">Open Risk Cases</div>
        <div style="font-size:1.75rem;font-weight:700;color:#ea580c;">{open_cases}</div>
    </div>
    """, unsafe_allow_html=True)
    st.caption("Cases under investigation or awaiting human decision.")
with kpi_cols[5]:
    st.markdown(f"""
    <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:14px 16px;">
        <div style="font-size:0.875rem;color:#6b7280;font-weight:400;">Awaiting Decision</div>
        <div style="font-size:1.75rem;font-weight:700;color:#dc2626;">{actions_pending}</div>
    </div>
    """, unsafe_allow_html=True)
    st.caption("Recommended actions awaiting approval from assigned persona.")

st.divider()

# ============================================================
# SECTION 2: PRIORITY FINDINGS — Tabs by Event Category
# ============================================================

st.subheader("Priority Findings")
st.caption("Top procurement risks grouped by category. Procurement Managers and Finance Directors should review critical/high items and take action.")

try:
    df_cases = get_cases(limit=50)
except Exception as e:
    st.error(f"Error loading cases: {e}")
    df_cases = None

# Also get all events for tabs (including those without cases yet)
try:
    all_events_df = run_query_df(f"""
        SELECT SEVERITY, ENTITY_KEY, EVENT_TYPE, IMPACT_USD, STATUS, HEADLINE
        FROM {DATABASE}.ACTION.BUSINESS_EVENT
        WHERE RUN_ID = (SELECT MAX(RUN_ID) FROM {DATABASE}.ACTION.BUSINESS_EVENT)
        ORDER BY ABS(IMPACT_USD) DESC
    """)
except Exception as e:
    st.error(f"Error loading events: {e}")
    import pandas as pd
    all_events_df = pd.DataFrame()

# Determine event categories for tabs
if not all_events_df.empty:
    event_types = all_events_df["EVENT_TYPE"].unique().tolist()
else:
    event_types = []

CATEGORY_LABELS = {
    "invoice_over_po": "Invoice Over PO",
    "grir_aging": "GR/IR Aging",
    "ap_open_item_aging": "AP Aging",
    "duplicate_invoice_receipt": "Duplicate Invoice",
    "po_invoice_currency_mismatch": "Currency Mismatch",
    "unusual_payment_terms": "Payment Terms",
}

BRANCH_LABELS = {
    "goods_receipt_no_invoice": "Uninvoiced Goods (GR without Invoice)",
    "price_variance": "Price Variance / Overbilling",
    "duplicate_ir": "Duplicate Invoice Receipt",
    "no_goods_receipt": "Missing Goods Receipt",
    "currency_control_gap": "Currency Control Gap",
    "payment_terms_drift": "Payment Terms Deviation",
    "over_delivery": "Over-Delivery",
    "indeterminate": "Indeterminate (Needs Review)",
}

OWNER_LABELS = {
    "invoice_over_po": "Procurement Manager",
    "grir_aging": "Supply Chain Leader",
    "ap_open_item_aging": "Finance Manager",
    "duplicate_invoice_receipt": "Finance Manager",
    "po_invoice_currency_mismatch": "Procurement Analyst",
    "unusual_payment_terms": "Procurement Analyst",
}

# Persona-based tab mapping
PERSONA_TAB_CONFIG = {
    "procurement_manager": {"label": "Procurement Manager", "types": ["invoice_over_po", "grir_aging"]},
    "finance_manager": {"label": "Finance Manager", "types": ["ap_open_item_aging", "grir_aging", "duplicate_invoice_receipt"]},
    "supply_chain_leader": {"label": "Supply Chain Leader", "types": ["grir_aging", "invoice_over_po"]},
    "procurement_analyst": {"label": "Procurement Analyst", "types": ["unusual_payment_terms", "po_invoice_currency_mismatch"]},
    "category_manager": {"label": "Category Manager", "types": ["invoice_over_po"]},
    "cfo_coo": {"label": "CFO / COO", "types": ["invoice_over_po", "duplicate_invoice_receipt", "grir_aging"]},
}

# Build persona tabs
persona_keys = list(PERSONA_TAB_CONFIG.keys())
persona_labels = [PERSONA_TAB_CONFIG[k]["label"] for k in persona_keys]
persona_tabs = st.tabs(persona_labels)

for tab_idx, persona_key in enumerate(persona_keys):
    with persona_tabs[tab_idx]:
        config = PERSONA_TAB_CONFIG[persona_key]
        st.caption(f"Cases assigned to **{config['label']}** for review and action.")

        # Get cases for this persona
        if not df_cases.empty:
            persona_cases = df_cases[df_cases["OWNER"] == persona_key]
        else:
            persona_cases = pd.DataFrame()

        if not persona_cases.empty:
            # Header row
            hcol1, hcol2, hcol3, hcol4, hcol5 = st.columns([1, 2, 3, 2, 2])
            with hcol1:
                st.markdown("**Priority**")
            with hcol2:
                st.markdown("**Supplier**")
            with hcol3:
                st.markdown("**Impact — Type**")
            with hcol4:
                st.markdown("**Status**")
            with hcol5:
                st.markdown("**Action**")

            for _, row in persona_cases.iterrows():
                case_id = row["CASE_ID"]
                col1, col2, col3, col4, col5 = st.columns([1, 2, 3, 2, 2])
                with col1:
                    sev = row.get("RISK_LEVEL", "MEDIUM")
                    color = {"CRITICAL": "#dc2626", "HIGH": "#ea580c", "MEDIUM": "#2563eb"}.get(sev, "#666")
                    st.markdown(f'<span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">{sev}</span>', unsafe_allow_html=True)
                with col2:
                    st.markdown(f"**{row.get('VENDOR_NAME', '')}**")
                    st.caption(f"{case_id}")
                with col3:
                    impact = float(row.get("FINANCIAL_IMPACT") or 0)
                    impact_str = f"${impact/1e6:.1f}M" if impact >= 1e6 else f"${impact:,.0f}"
                    case_type_label = CATEGORY_LABELS.get(row.get("CASE_TYPE", ""), row.get("CASE_TYPE", ""))
                    st.caption(f"{impact_str} — {case_type_label}")
                with col4:
                    status_raw = row.get("STATUS", "")
                    status_labels = {
                        "AI_INVESTIGATED": "AI Investigated",
                        "AWAITING_DECISION": "Awaiting Decision",
                        "NEW": "New",
                        "ACTION_EXECUTED": "Action Executed",
                        "RESOLVED": "Resolved",
                        "INVESTIGATING": "Investigating",
                    }
                    status_label = status_labels.get(status_raw, status_raw)
                    confidence = row.get("RISK_SCORE", "")
                    conf_str = f"{float(confidence):.0f}%" if confidence else ""
                    st.markdown(f"**{status_label}**")
                    if status_raw == "AI_INVESTIGATED" and conf_str:
                        st.caption(f"Root cause identified · Confidence {conf_str}")
                    elif status_raw == "AWAITING_DECISION":
                        st.caption("Pending human approval")
                    elif status_raw == "NEW":
                        st.caption("Awaiting investigation")
                with col5:
                    if row.get("STATUS") in ("AI_INVESTIGATED", "AWAITING_DECISION", "NEW"):
                        if st.button("📂 View Case", key=f"act_{case_id}_{tab_idx}", type="secondary"):
                            st.session_state["selected_case"] = case_id
                            st.toast(f"Viewing case {case_id} — scroll down for details", icon="👇")
                            st.rerun()
                    elif row.get("STATUS") == "ACTION_EXECUTED":
                        st.caption("Executed ✓")
                    elif row.get("STATUS") == "RESOLVED":
                        st.caption("Resolved ✓")
                    else:
                        st.caption(row.get("STATUS", ""))
        else:
            # No cases directly owned — show cases matching this persona's event types
            relevant_types = config["types"]
            if not df_cases.empty:
                type_cases = df_cases[df_cases["CASE_TYPE"].isin(relevant_types)]
            else:
                type_cases = pd.DataFrame()

            if not type_cases.empty:
                # Show cases with View Case button (same layout as owner tab)
                hcol1, hcol2, hcol3, hcol4, hcol5 = st.columns([1, 2, 3, 2, 2])
                with hcol1:
                    st.markdown("**Priority**")
                with hcol2:
                    st.markdown("**Supplier**")
                with hcol3:
                    st.markdown("**Impact — Type**")
                with hcol4:
                    st.markdown("**Status**")
                with hcol5:
                    st.markdown("**Action**")

                for _, row in type_cases.head(5).iterrows():
                    case_id = row["CASE_ID"]
                    col1, col2, col3, col4, col5 = st.columns([1, 2, 3, 2, 2])
                    with col1:
                        sev = row.get("RISK_LEVEL", "MEDIUM")
                        color = {"CRITICAL": "#dc2626", "HIGH": "#ea580c", "MEDIUM": "#2563eb"}.get(sev, "#666")
                        st.markdown(f'<span style="background:{color};color:white;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;">{sev}</span>', unsafe_allow_html=True)
                    with col2:
                        st.markdown(f"**{row.get('VENDOR_NAME', '')}**")
                        st.caption(f"{case_id}")
                    with col3:
                        impact = float(row.get("FINANCIAL_IMPACT") or 0)
                        impact_str = f"${impact/1e6:.1f}M" if impact >= 1e6 else f"${impact:,.0f}"
                        case_type_label = CATEGORY_LABELS.get(row.get("CASE_TYPE", ""), row.get("CASE_TYPE", ""))
                        st.caption(f"{impact_str} — {case_type_label}")
                    with col4:
                        status_raw = row.get("STATUS", "")
                        status_labels = {
                            "AI_INVESTIGATED": "AI Investigated",
                            "AWAITING_DECISION": "Awaiting Decision",
                            "NEW": "New",
                            "ACTION_EXECUTED": "Action Executed",
                            "RESOLVED": "Resolved",
                            "INVESTIGATING": "Investigating",
                        }
                        status_label = status_labels.get(status_raw, status_raw)
                        confidence = row.get("RISK_SCORE", "")
                        conf_str = f"{float(confidence):.0f}%" if confidence else ""
                        st.markdown(f"**{status_label}**")
                        if status_raw == "AI_INVESTIGATED" and conf_str:
                            st.caption(f"Root cause identified · Confidence {conf_str}")
                        elif status_raw == "AWAITING_DECISION":
                            st.caption("Pending human approval")
                        elif status_raw == "NEW":
                            st.caption("Awaiting investigation")
                    with col5:
                        if st.button("📂 View Case", key=f"act_{case_id}_{tab_idx}_alt", type="secondary"):
                            st.session_state["selected_case"] = case_id
                            st.toast(f"Viewing case {case_id} — scroll down for details", icon="👇")
                            st.rerun()
            elif not all_events_df.empty:
                cat_events = all_events_df[all_events_df["EVENT_TYPE"].isin(relevant_types)]
                if not cat_events.empty:
                    display = cat_events[["SEVERITY", "ENTITY_KEY", "EVENT_TYPE", "IMPACT_USD", "HEADLINE"]].head(5).copy()
                    display["EVENT_TYPE"] = display["EVENT_TYPE"].map(CATEGORY_LABELS).fillna(display["EVENT_TYPE"])
                    display["IMPACT_USD"] = display["IMPACT_USD"].apply(lambda x: f"${float(x or 0):,.0f}")
                    display.columns = ["Severity", "Entity", "Type", "Impact", "Headline"]
                    st.dataframe(display, use_container_width=True, hide_index=True)
                else:
                    st.info(f"No events for {config['label']}.")
            else:
                st.info("No events detected yet.")

st.divider()

# ============================================================
# SECTION 3: RUN AI INVESTIGATION (below findings)
# ============================================================

# Run configuration
with st.expander("⚙️ Investigation Settings", expanded=False):
    cfg_col1, cfg_col2, cfg_col3 = st.columns(3)
    with cfg_col1:
        run_scope = st.selectbox("Scope", ["Quick (Top 3)", "Standard (Top 5)", "Deep (Top 10)"],
                                 index=1, help="Number of anomalies to investigate")
    with cfg_col2:
        run_detectors = st.multiselect("Detectors", 
                                        ["Invoice Over PO", "GR/IR Aging", "AP Aging",
                                         "Duplicate Invoice", "Currency Mismatch", "Payment Terms Drift"],
                                        default=["Invoice Over PO", "GR/IR Aging", "AP Aging"],
                                        help="Which detector views to scan")
    with cfg_col3:
        run_period = st.selectbox("Period", ["Last 1 Month", "Last 3 Months", "Last 6 Months", "Last 1 Year"],
                                  index=0, help="Time window for detection")

# Map UI choices to params
scope_map = {"Quick (Top 3)": 3, "Standard (Top 5)": 5, "Deep (Top 10)": 10}
run_limit = scope_map.get(run_scope, 5)
detector_map = {
    "Invoice Over PO": "invoice_over_po",
    "GR/IR Aging": "grir_aging",
    "AP Aging": "ap_open_item_aging",
    "Duplicate Invoice": "duplicate_invoice_receipt",
    "Currency Mismatch": "po_invoice_currency_mismatch",
    "Payment Terms Drift": "unusual_payment_terms",
}
active_detectors = [detector_map[d] for d in run_detectors]
period_map = {"Last 1 Month": 30, "Last 3 Months": 90, "Last 6 Months": 180, "Last 1 Year": 365}
run_period_days = period_map.get(run_period)

col_left, col_center, col_right = st.columns([1, 1, 1])
with col_left:
    st.markdown("""
    <style>
    /* Run AI Investigation button - purple/blue gradient */
    div[data-testid="column"]:nth-child(1) button[data-testid="stBaseButton-primary"] {
        background: linear-gradient(135deg, #7c3aed, #2563eb) !important;
        color: white !important;
        border: none !important;
        font-weight: 700;
        border-radius: 8px;
        font-size: 15px;
        padding: 12px 24px;
    }
    </style>
    """, unsafe_allow_html=True)
    run_clicked = st.button("🚀 Run AI Investigation", type="primary", use_container_width=True,
                            help="Run investigation on top of existing data (appends new findings)")
with col_center:
    st.markdown("""
    <style>
    /* Reset & Run button - red/orange gradient */
    div[data-testid="column"]:nth-child(2) button[data-testid="stBaseButton-secondary"] {
        background: linear-gradient(135deg, #dc2626, #ea580c) !important;
        color: white !important;
        border: none !important;
        font-weight: 700;
        border-radius: 8px;
        font-size: 14px;
        padding: 12px 24px;
    }
    </style>
    """, unsafe_allow_html=True)
    reset_clicked = st.button("🚀 Run AI Investigation (reset old data)", use_container_width=True,
                              help="Clears ALL previous investigation data and runs a fresh AI investigation from scratch")

st.markdown(f"""
<div style="text-align:center;margin-top:4px;margin-bottom:16px;font-size:12px;color:#6b7280;line-height:1.6;white-space:nowrap;">
5 agents · Top {run_limit} anomalies · {len(active_detectors)} detectors · {run_period} &nbsp;|&nbsp; Detect → Investigate (Five-Why) → Risk Score → Plan Actions → Create Cases
</div>
""", unsafe_allow_html=True)

if reset_clicked:
    rid = next_run_id()
    with st.status("🔄 Resetting & Running Fresh Investigation...", expanded=True) as status:
        st.write("🗑️ **Clearing previous investigation data...**")
        try:
            tables_cleared = reset_action_schema(conn)
            st.write(f"✅ Cleared {tables_cleared} ACTION tables")
        except Exception as e:
            st.error(f"❌ Reset failed: {e}")
            status.update(label="❌ Reset failed", state="error")
            st.stop()

        st.write("▶ **Starting fresh AI Investigation...**")

        agent_steps = {
            "phase1": ("🔍 Detection Agent", "Scanning detector views for anomalies..."),
            "phase2": ("🧠 Investigation Agent", "Five-Why root cause reasoning with evidence traversal..."),
            "phase3": ("⚖️ Risk & Decision Agent", "Scoring risk, predicting cascade, deciding priority..."),
            "phase4": ("📋 Planning Agent", "Selecting actions from catalog, gating approvals..."),
            "phase5": ("📁 Case Creation Agent", "Creating cases, writing audit trail..."),
        }

        def progress_cb(phase_key, message):
            if phase_key.endswith("_start"):
                phase_num = phase_key.replace("_start", "")
                agent_name, desc = agent_steps[phase_num]
                st.write(f"▶ **{agent_name}**")
                st.caption(f"  {desc}")
            elif phase_key.endswith("_done"):
                phase_num = phase_key.replace("_done", "")
                agent_name, _ = agent_steps[phase_num]
                st.write(f"✅ **{agent_name}** — {message}")
            elif phase_key.endswith("_error"):
                st.error(f"  ❌ Error: {message}")

        result = run_full_pipeline_with_cases(conn, rid, progress_callback=progress_cb, limit=run_limit,
                                                    detectors=active_detectors, period_days=run_period_days)

        if result["errors"]:
            status.update(label="⚠️ Investigation completed with errors", state="error")
            for err in result["errors"]:
                st.error(err)
        else:
            cases = result.get("cases_created", 0)
            events = result.get("events_detected", 0)
            status.update(label=f"✅ Fresh Investigation Complete — {cases} cases created", state="complete")

    st.session_state["last_run_summary"] = {
        "events_detected": result.get("events_detected", 0),
        "investigations": result.get("investigations", 0),
        "actions_planned": result.get("actions_planned", 0),
        "cases_created": result.get("cases_created", 0),
        "emails_sent": result.get("emails_sent", 0),
    }
    st.rerun()

if run_clicked:
    rid = next_run_id()
    with st.status("🤖 Agentic Workflow Running...", expanded=True) as status:
        agent_steps = {
            "phase1": ("🔍 Detection Agent", "Scanning 6 detector views for anomalies..."),
            "phase2": ("🧠 Investigation Agent", "Five-Why root cause reasoning with evidence traversal..."),
            "phase3": ("⚖️ Risk & Decision Agent", "Scoring risk, predicting cascade, deciding priority..."),
            "phase4": ("📋 Planning Agent", "Selecting actions from catalog, gating approvals..."),
            "phase5": ("📁 Case Creation Agent", "Creating cases, writing audit trail..."),
        }

        reasoning_text = {
            "phase2": [
                "**Reasoning:** WHY is invoiced amount > PO value? → WHY is GR > IR? → WHAT is the real risk?",
                "Traversing: Vendor → PO → GR/IR → Inventory → AP → GL (7-hop evidence graph)",
                "Generating ranked hypotheses with confidence scores...",
            ],
            "phase3": [
                "**Decision logic:** Composite score = financial_weight × severity + cascade_probability",
                "Evaluating: Which plants stop? Which product lines affected? Single-source risk?",
                "Autonomous decision: Assign P1/P2/P3 priority, route to responsible persona",
            ],
        }

        def progress_cb(phase_key, message):
            if phase_key.endswith("_start"):
                phase_num = phase_key.replace("_start", "")
                agent_name, desc = agent_steps[phase_num]
                st.write(f"▶ **{agent_name}**")
                st.caption(f"  {desc}")
                # Show reasoning steps for investigation and decision phases
                if phase_num in reasoning_text:
                    for line in reasoning_text[phase_num]:
                        st.caption(f"  ↳ {line}")
            elif phase_key.endswith("_done"):
                phase_num = phase_key.replace("_done", "")
                agent_name, _ = agent_steps[phase_num]
                st.write(f"✅ **{agent_name}** — {message}")
            elif phase_key.endswith("_error"):
                st.error(f"  ❌ Error: {message}")

        result = run_full_pipeline_with_cases(conn, rid, progress_callback=progress_cb, limit=run_limit,
                                                    detectors=active_detectors, period_days=run_period_days)

        if result["errors"]:
            status.update(label="⚠️ Investigation completed with errors", state="error")
            for err in result["errors"]:
                st.error(err)
        else:
            cases = result.get("cases_created", 0)
            events = result.get("events_detected", 0)
            status.update(label=f"✅ Agentic Workflow Complete", state="complete")

    # Show final summary outside the status block so it's always visible
    st.session_state["last_run_summary"] = {
        "events_detected": result.get("events_detected", 0),
        "investigations": result.get("investigations", 0),
        "actions_planned": result.get("actions_planned", 0),
        "cases_created": result.get("cases_created", 0),
        "emails_sent": result.get("emails_sent", 0),
    }
    st.rerun()

# Show persistent summary from last run
if "last_run_summary" in st.session_state:
    s = st.session_state["last_run_summary"]
    st.success(f"""
**✅ Agentic Workflow Complete**  
- 🔍 **{s['events_detected']}** anomalies detected  
- 🧠 **{s['investigations']}** root causes investigated (Five-Why reasoning)  
- ⚖️ Risk scored and priority assigned (autonomous decision)  
- 📋 **{s['actions_planned']}** actions planned  
- 📁 **{s['cases_created']}** cases created with full audit trail  
- 📧 **{s.get('emails_sent', 0)}** email alerts sent to assigned personas  
    """)
    st.info("↓ Scroll down to **Select Case to Investigate** to review findings and take action.")
    if st.button("Dismiss", key="dismiss_summary"):
        del st.session_state["last_run_summary"]
        st.rerun()

# ============================================================
# SECTION 4: CASE DETAIL (selected via Take Action button or dropdown)
# ============================================================

# Determine which case to show
selected_case_id = st.session_state.get("selected_case", None)

if not df_cases.empty:
    st.divider()
    case_ids = df_cases["CASE_ID"].tolist()
    case_labels = [f"{row['CASE_ID']} — {row['VENDOR_NAME']} ({row['RISK_LEVEL']})" for _, row in df_cases.iterrows()]

    # Pre-select the case from button click
    default_idx = 0
    if selected_case_id and selected_case_id in case_ids:
        default_idx = case_ids.index(selected_case_id)

    st.subheader("Select Case to Investigate")
    selected_idx = st.selectbox("", range(len(case_labels)),
                                index=default_idx, format_func=lambda i: case_labels[i])
    selected_case_id = case_ids[selected_idx]
    case = get_case(selected_case_id)

    if case:
        st.subheader(f"Case: {case['CASE_ID']}")

        status_color = {"CRITICAL": "red", "HIGH": "orange", "MEDIUM": "blue"}.get(case.get("RISK_LEVEL"), "gray")
        st.markdown(f"""
        <div style="display:flex;gap:12px;align-items:center;margin-bottom:16px;">
            <span style="background:{status_color};color:white;padding:4px 10px;border-radius:4px;font-weight:600;font-size:12px;">{case.get('RISK_LEVEL','')}</span>
            <span style="background:#2d3748;color:white;padding:4px 10px;border-radius:4px;font-size:12px;">{case.get('STATUS','')}</span>
            <span style="color:#666;font-size:13px;">Exposure: <b>${float(case.get('FINANCIAL_IMPACT') or 0):,.0f}</b></span>
        </div>
        """, unsafe_allow_html=True)

        detail_cols = st.columns(3)
        with detail_cols[0]:
            st.markdown(f"**Supplier:** {case.get('VENDOR_NAME', '')}")
            st.markdown(f"**Issue:** {CATEGORY_LABELS.get(case.get('CASE_TYPE', ''), case.get('CASE_TYPE', ''))}")
        with detail_cols[1]:
            from config.personas import PERSONAS
            owner_display = PERSONAS.get(case.get('OWNER', ''), case.get('OWNER', ''))
            st.markdown(f"**Risk Score:** {case.get('RISK_SCORE', 'N/A')}")
            st.markdown(f"**Owner:** {owner_display}")
        with detail_cols[2]:
            st.markdown(f"**Root Cause:** {BRANCH_LABELS.get(case.get('ROOT_CAUSE', ''), case.get('ROOT_CAUSE', 'Pending'))}")
            st.markdown(f"**Created:** {str(case.get('CREATED_AT', ''))[:16]}")

        # Five-Why Narrative
        if case.get("INVESTIGATION_ID"):
            inv_data = run_query(f"""
                SELECT NARRATIVE, HYPOTHESES, CONFIDENCE, ROOT_CAUSE_BRANCH
                FROM {DATABASE}.ACTION.INVESTIGATION
                WHERE INVESTIGATION_ID = {case['INVESTIGATION_ID']}
            """)
            if inv_data:
                inv = inv_data[0]
                with st.expander("Five-Why Analysis", expanded=True):
                    narrative = inv.get("NARRATIVE", "")
                    import re
                    # Try splitting on newlines first (pipeline format), then on (1)/(2) markers (CoCo format)
                    lines = [l.strip() for l in narrative.split("\n") if l.strip()]
                    if len(lines) >= 3:
                        steps = lines
                    else:
                        steps = [s.strip() for s in re.split(r'\(\d+\)\s*', narrative) if s.strip()]

                    if steps:
                        why_num = 0
                        for step in steps:
                            escaped = step.replace('$', '\\$')
                            # Strip leading "N." if already numbered
                            escaped = re.sub(r'^\d+\.\s*', '', escaped)
                            if not escaped:
                                continue

                            # First line is title/header (contains "Five-Why" or "Investigation for") — show without number
                            if why_num == 0 and ("five-why" in escaped.lower() or "investigation for" in escaped.lower()):
                                st.markdown(f"**{escaped}**")
                                continue

                            why_num += 1

                            # Split question from answer on "?" boundary
                            if "?" in escaped and not escaped.endswith("?"):
                                q_idx = escaped.index("?") + 1
                                question = escaped[:q_idx].strip()
                                answer = escaped[q_idx:].strip()
                                st.markdown(f"**{why_num}.** {question} <span style='color:#2563eb;'>{answer}</span>", unsafe_allow_html=True)
                            elif "→" in escaped:
                                parts = escaped.split("→", 1)
                                st.markdown(f"**{why_num}.** {parts[0].strip()} → <span style='color:#2563eb;'>{parts[1].strip()}</span>", unsafe_allow_html=True)
                            elif escaped.lower().startswith("business impact"):
                                # Business Impact is the conclusion — highlight in bold blue
                                st.markdown(f"**{why_num}. 💡** <span style='color:#1e40af;font-weight:600;'>{escaped}</span>", unsafe_allow_html=True)
                            else:
                                st.markdown(f"**{why_num}.** {escaped}")
                    else:
                        st.markdown(f"- {narrative.replace('$', chr(92)+'$')}")

                with st.expander("Ranked Hypotheses"):
                    hyps = inv.get("HYPOTHESES")
                    if hyps:
                        try:
                            hyp_list = json.loads(hyps) if isinstance(hyps, str) else hyps
                            if isinstance(hyp_list, list):
                                for h in hyp_list:
                                    score = float(h.get("score", 0)) * 100
                                    reason = h.get("reason", "").replace("$", "\\$")
                                    branch_label = BRANCH_LABELS.get(h.get("branch", ""), h.get("branch", ""))
                                    st.markdown(f"**{branch_label}** ({score:.0f}%) — {reason}")
                            else:
                                st.markdown(str(hyps).replace("$", "\\$"))
                        except (json.JSONDecodeError, TypeError):
                            # Plain text hypotheses - display as-is
                            for line in str(hyps).split("\n"):
                                if line.strip():
                                    st.markdown(f"**{line.strip().replace('$', chr(92)+'$')}**" if line[0].isalpha() else line.replace("$", "\\$"))

        # Recommendation + Action
        if case.get("RECOMMENDATION"):
            st.divider()
            st.markdown(f"**AI Recommendation:** {case['RECOMMENDATION'].replace('$', '\\$')}")

            if case.get("STATUS") in ("AI_INVESTIGATED", "AWAITING_DECISION"):
                action_cols = st.columns(3)
                with action_cols[0]:
                    if st.button("Execute Payment Hold", key=f"hold_{case['CASE_ID']}", type="primary"):
                        execute_action(case["CASE_ID"], "payment_hold", persona)
                        audit_log(case["CASE_ID"], "ACTION_EXECUTED", "USER", persona,
                                  f"Payment Hold executed. Impact: \\${float(case.get('FINANCIAL_IMPACT') or 0):,.0f}")
                        st.toast(f"Payment Hold placed on {case['VENDOR_NAME']} — \\${float(case.get('FINANCIAL_IMPACT') or 0):,.0f} blocked", icon="🛑")
                        st.rerun()
                with action_cols[1]:
                    if st.button("Create Investigation Task", key=f"task_{case['CASE_ID']}"):
                        update_case_status(case["CASE_ID"], "AWAITING_DECISION")
                        audit_log(case["CASE_ID"], "TASK_CREATED", "USER", persona,
                                  "Investigation task created for further review")
                        st.toast(f"Task created for {case['VENDOR_NAME']} — assigned to {case.get('OWNER', 'team')}", icon="📋")
                        st.rerun()
                with action_cols[2]:
                    if st.button("Resolve / Dismiss", key=f"resolve_{case['CASE_ID']}"):
                        update_case_status(case["CASE_ID"], "RESOLVED")
                        audit_log(case["CASE_ID"], "STATUS_CHANGED", "USER", persona,
                                  "Case resolved/dismissed by user")
                        st.toast(f"Case {case['CASE_ID']} resolved", icon="✅")
                        st.rerun()
            elif case.get("STATUS") == "ACTION_EXECUTED":
                st.success(f"**PAYMENT HOLD ACTIVE** — \\${float(case.get('FINANCIAL_IMPACT') or 0):,.0f} blocked for {case.get('VENDOR_NAME', '')}. Audit trail recorded.")
            elif case.get("STATUS") == "RESOLVED":
                owner_raw = case.get("OWNER", "user")
                owner_friendly = owner_raw.replace("_", " ").title() if owner_raw else "User"
                st.info(f"**RESOLVED** — Case closed by {owner_friendly}.")

        # Audit Timeline
        st.divider()
        with st.expander("Audit Trail"):
            audit_df = get_audit_trail(case["CASE_ID"])
            if not audit_df.empty:
                persona_display_map = {
                    "procurement_manager": "Procurement Manager",
                    "finance_manager": "Finance Manager",
                    "supply_chain_leader": "Supply Chain Leader",
                    "category_manager": "Category Manager",
                    "cfo_coo": "CFO / COO",
                    "ap_manager": "AP Manager",
                    "ap_clerk": "AP Clerk",
                    "controller": "Controller",
                    "buyer": "Buyer",
                    "Investigation Agent": "Investigation Agent",
                    "Detection Agent": "Detection Agent",
                }
                for _, row in audit_df.iterrows():
                    ts = str(row.get("CREATED_AT", ""))[:16]
                    actor_raw = row.get("ACTOR", "System")
                    actor = persona_display_map.get(actor_raw, actor_raw.replace("_", " ").title())
                    desc = row.get("DESCRIPTION", "")
                    # Map technical names to business-friendly labels in description
                    desc_mappings = {
                        "ap_manager": "AP Manager", "ap_clerk": "AP Clerk",
                        "category_manager": "Category Manager", "controller": "Financial Controller",
                        "buyer": "Buyer", "procurement_head": "Head of Procurement",
                        "cfo": "CFO", "plant_manager": "Plant Manager",
                        "goods_receipt_no_invoice": "Uninvoiced Goods Receipt",
                        "no_goods_receipt": "Missing Goods Receipt / AP Aging",
                        "price_variance": "Vendor Price Variance / Overbilling",
                        "duplicate_ir": "Duplicate Invoice Receipt",
                        "currency_control_gap": "Currency Control Gap",
                        "payment_terms_drift": "Payment Terms Deviation",
                        "over_delivery": "Over-Delivery",
                        "indeterminate": "Indeterminate",
                    }
                    for tech, friendly in desc_mappings.items():
                        desc = desc.replace(tech, friendly)
                    icon = {"SYSTEM": "🔧", "AI_AGENT": "🤖", "USER": "👤"}.get(row.get("ACTOR_TYPE", ""), "•")
                    st.markdown(f"`{ts}` {icon} **{actor}** — {desc}")
            else:
                st.caption("No audit events yet.")

        # Export
        st.divider()
        export_cols = st.columns(4)
        with export_cols[0]:
            report_html = _generate_case_report(case)
            st.download_button("Export HTML Report", report_html,
                               file_name=f"report_{case['CASE_ID']}.html",
                               mime="text/html")

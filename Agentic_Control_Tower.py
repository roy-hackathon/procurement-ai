import streamlit as st
import pandas as pd
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import get_snowflake_connection, is_connected, run_query_df, run_query
from services.pipeline import run_full_pipeline_with_cases, next_run_id, DATABASE
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
persona = persona_selector()

st.markdown("""
<div style="background:#f8fafc;border-left:4px solid #2563eb;padding:12px 16px;margin-bottom:20px;border-radius:4px;font-size:14px;">
<b>Detect → Investigate → Decide → Act → Audit</b><br>
This Control Tower monitors CoCoEV's procurement risks across 3 plants and 10 suppliers.
AI agents detect anomalies, investigate root causes with Five-Why reasoning, recommend actions,
and enable one-click execution — all with full audit trail for governance.
</div>
""", unsafe_allow_html=True)

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

conn = get_snowflake_connection()

# Card border styling + colored metrics for risk/action cards
st.markdown("""
<style>
[data-testid="stMetric"] {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
div[data-testid="column"]:nth-child(5) [data-testid="stMetricValue"] {
    color: #ea580c;
}
div[data-testid="column"]:nth-child(6) [data-testid="stMetricValue"] {
    color: #dc2626;
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
            hyp_list = json.loads(hyps) if isinstance(hyps, str) else hyps
            for h in hyp_list:
                score = float(h.get("score", 0)) * 100
                hyp_html += f"<tr><td>{h.get('branch','')}</td><td>{score:.0f}%</td><td>{h.get('reason','')}</td></tr>"

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
    exp_str = f"${total_exposure/1e6:.1f}M"
    st.metric("Financial Exposure", exp_str)
    st.caption("Total at-risk amount from AI-detected anomalies requiring action.")
with kpi_cols[4]:
    st.metric("Open Risk Cases", open_cases)
    st.caption("Cases under investigation or awaiting human decision.")
with kpi_cols[5]:
    st.metric("Actions Pending", actions_pending)
    st.caption("Recommended actions awaiting approval from assigned persona.")

st.divider()

# ============================================================
# SECTION 2: PRIORITY FINDINGS — Tabs by Event Category
# ============================================================

st.subheader("Priority Findings")
st.caption("Top procurement risks grouped by category. Procurement Managers and Finance Directors should review critical/high items and take action.")

df_cases = get_cases(limit=50)

# Also get all events for tabs (including those without cases yet)
all_events_df = run_query_df(f"""
    SELECT SEVERITY, ENTITY_KEY, EVENT_TYPE, IMPACT_USD, STATUS, HEADLINE
    FROM {DATABASE}.ACTION.BUSINESS_EVENT
    WHERE RUN_ID = (SELECT MAX(RUN_ID) FROM {DATABASE}.ACTION.BUSINESS_EVENT)
    ORDER BY ABS(IMPACT_USD) DESC
""")

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
    "finance_manager": {"label": "Finance Manager", "types": ["ap_open_item_aging", "duplicate_invoice_receipt"]},
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
                    st.caption(f"{row.get('STATUS', '')}")
                with col5:
                    if row.get("STATUS") in ("AI_INVESTIGATED", "AWAITING_DECISION", "NEW"):
                        if st.button("Take Action", key=f"act_{case_id}_{tab_idx}", type="primary"):
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
            # Show raw events for this persona's event types
            relevant_types = config["types"]
            if not all_events_df.empty:
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

col_action, col_status = st.columns([1, 3])
with col_action:
    run_clicked = st.button("Run AI Investigation", type="primary", use_container_width=True)
with col_status:
    st.caption("Runs 5 agents: Detect → Investigate (Five-Why) → Risk Score → Plan Actions → Create Cases. Processes top 5 events.")

if run_clicked:
    rid = next_run_id()
    with st.status("Multi-Agent Investigation Running...", expanded=True) as status:
        agent_steps = {
            "phase1": ("Agent 1 — Risk Detection", None),
            "phase2": ("Agent 2 — Evidence Analysis (Five-Why)", None),
            "phase3": ("Agent 3 — Risk Scoring & Cascade", None),
            "phase4": ("Agent 4 — Action Planning", None),
            "phase5": ("Agent 5 — Case Creation & Audit", None),
        }

        def progress_cb(phase_key, message):
            if phase_key.endswith("_start"):
                phase_num = phase_key.replace("_start", "")
                agent_name = agent_steps[phase_num][0]
                st.write(f"● **{agent_name}**")
                st.caption(f"  {message}")
            elif phase_key.endswith("_done"):
                phase_num = phase_key.replace("_done", "")
                agent_name = agent_steps[phase_num][0]
                st.write(f"✓ **{agent_name}**")
                st.caption(f"  {message}")
            elif phase_key.endswith("_error"):
                st.error(f"  Error: {message}")

        result = run_full_pipeline_with_cases(conn, rid, progress_callback=progress_cb, limit=5)

        if result["errors"]:
            status.update(label="Investigation completed with errors", state="error")
            for err in result["errors"]:
                st.error(err)
        else:
            cases = result.get("cases_created", 0)
            status.update(label=f"Investigation Complete — {cases} cases created", state="complete")

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

    selected_idx = st.selectbox("Select Case to Investigate", range(len(case_labels)),
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
                    for line in narrative.split("\n"):
                        if line.strip():
                            escaped = line.strip().replace('$', '\\$')
                            # Split on "→" or "?" to separate question from answer
                            if "→" in escaped:
                                parts = escaped.split("→", 1)
                                st.markdown(f"- {parts[0]}→ <span style='color:#2563eb;'>{parts[1]}</span>", unsafe_allow_html=True)
                            elif "?" in escaped and not escaped.endswith("?"):
                                idx = escaped.index("?") + 1
                                question = escaped[:idx]
                                answer = escaped[idx:].strip()
                                st.markdown(f"- {question} <span style='color:#2563eb;'>{answer}</span>", unsafe_allow_html=True)
                            else:
                                st.markdown(f"- {escaped}")

                with st.expander("Ranked Hypotheses"):
                    hyps = inv.get("HYPOTHESES")
                    if hyps:
                        hyp_list = json.loads(hyps) if isinstance(hyps, str) else hyps
                        for h in hyp_list:
                            score = float(h.get("score", 0)) * 100
                            reason = h.get("reason", "").replace("$", "\\$")
                            branch_label = BRANCH_LABELS.get(h.get("branch", ""), h.get("branch", ""))
                            st.markdown(f"**{branch_label}** ({score:.0f}%) — {reason}")

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
                st.info(f"**RESOLVED** — Case closed by {case.get('OWNER', 'user')}.")

        # Audit Timeline
        st.divider()
        with st.expander("Audit Trail"):
            audit_df = get_audit_trail(case["CASE_ID"])
            if not audit_df.empty:
                for _, row in audit_df.iterrows():
                    ts = str(row.get("CREATED_AT", ""))[:16]
                    actor = row.get("ACTOR", "System")
                    desc = row.get("DESCRIPTION", "")
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

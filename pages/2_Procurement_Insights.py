import streamlit as st
from decimal import Decimal
import pandas as pd
import altair as alt
import sys, os
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("procurement_insights")

from services.snowflake_connection import is_connected, run_query_df
from services.queries import (
    get_filter_options, get_kpi_metrics, get_spend_trend,
    get_spend_by_vendor, get_order_pipeline, get_spend_by_region
)
from config.settings import FISCAL_YEARS, DEFAULT_YEAR, CURRENT_YEAR, APP_VERSION
from components.persona import persona_selector

st.set_page_config(page_title="Procurement Insights", layout="wide", initial_sidebar_state="expanded")

# Clear stale cache on first load after deploy
if "cache_cleared" not in st.session_state:
    st.cache_data.clear()
    st.session_state["cache_cleared"] = True

st.title("Procurement Insights")
st.caption("KPIs, spend trends, and gap analysis across vendors, plants, and material groups — and talk to your data using AI.")
# persona removed

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

from components.sidebar_info import render_account_info
render_account_info()


def to_float(val):
    if val is None:
        return 0.0
    if isinstance(val, Decimal):
        return float(val)
    return float(val)


def fmt_money(val):
    v = to_float(val)
    if abs(v) >= 1e9:
        return f"${v/1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.1f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:,.0f}"


def calc_delta(current, previous):
    c = to_float(current)
    p = to_float(previous)
    if p == 0:
        return None
    return f"{((c - p) / p) * 100:.1f}%"


# --- Filters ---
vendors_list, plants_list, categories_list = get_filter_options()

vendor_options = {v["VENDOR_NAME"]: v["VENDOR_SK"] for v in vendors_list}
plant_options = {f"{p['PLANT_NAME']} ({p['PLANT_ID']})": p["PLANT_SK"] for p in plants_list}
category_map = {c["CATEGORY_LABEL"]: c["MATERIAL_GROUP"] for c in categories_list}

filter_cols = st.columns(4)
with filter_cols[0]:
    selected_year = st.selectbox("Fiscal Year", FISCAL_YEARS, index=FISCAL_YEARS.index(DEFAULT_YEAR))
with filter_cols[1]:
    selected_vendors = st.multiselect("Vendor", options=list(vendor_options.keys()), default=None,
                                       placeholder="All Vendors")
with filter_cols[2]:
    selected_plants = st.multiselect("Plant", options=list(plant_options.keys()), default=None,
                                      placeholder="All Plants")
with filter_cols[3]:
    selected_categories = st.multiselect("Category", options=list(category_map.keys()), default=None,
                                          placeholder="All Categories")

vendor_ids = [vendor_options[v] for v in selected_vendors] if selected_vendors else None
plant_ids = [plant_options[p] for p in selected_plants] if selected_plants else None
categories = [category_map[c] for c in selected_categories] if selected_categories else None

# --- YTD logic ---
is_ytd = (selected_year == CURRENT_YEAR)
ytd_month = 8 if is_ytd else None

if is_ytd:
    st.caption(f"Showing Year-to-Date (Jan-Aug {selected_year}). Prior year comparison uses the same period (Jan-Aug {selected_year - 1}).")

st.divider()

# --- KPI Cards with YoY trend ---
metrics = get_kpi_metrics(selected_year, vendor_ids, plant_ids, categories, max_month=ytd_month)
prev_year = selected_year - 1
metrics_prev = get_kpi_metrics(prev_year, vendor_ids, plant_ids, categories, max_month=ytd_month)

kpi_cols = st.columns(5)
with kpi_cols[0]:
    st.metric("Total Spend", fmt_money(metrics["total_spend"]),
              delta=calc_delta(metrics["total_spend"], metrics_prev["total_spend"]),
              help="Total AP (Accounts Payable) invoice spend by CoCoEV to all suppliers for the selected period across all 3 plants. Delta shows Year-over-Year change vs same period prior year.")
with kpi_cols[1]:
    st.metric("Invoices", f"{int(metrics['invoice_count']):,}",
              delta=calc_delta(metrics["invoice_count"], metrics_prev["invoice_count"]),
              help="Total number of AP (Accounts Payable) invoices received by CoCoEV from vendors. Each invoice is a payment request from a supplier for goods or services delivered.")
with kpi_cols[2]:
    st.metric("Purchase Orders", f"{int(metrics['po_count']):,}",
              delta=calc_delta(metrics["po_count"], metrics_prev["po_count"]),
              help="Distinct PO (Purchase Order) documents raised by CoCoEV procurement team. A PO is the commitment to buy, preceding goods receipt and invoice in the Procure-to-Pay cycle.")
with kpi_cols[3]:
    st.metric("PO Gross Value", fmt_money(metrics["po_gross"]),
              delta=calc_delta(metrics["po_gross"], metrics_prev["po_gross"]),
              help="Total gross value of all PO (Purchase Order) lines issued by CoCoEV. This is committed procurement spend before goods are received or invoices paid.")
with kpi_cols[4]:
    st.metric("Avg Invoice", fmt_money(metrics["avg_invoice"]),
              delta=calc_delta(metrics["avg_invoice"], metrics_prev["avg_invoice"]),
              help="Average AP (Accounts Payable) invoice amount across all CoCoEV vendors. A significant increase may indicate vendor price inflation or invoice batch consolidation.")

st.divider()


CORTEX_MODEL = "claude-opus-4-6"
COCOEV_CONTEXT = """CoCoEV is an Indian electric scooter manufacturer with 3 plants (Chennai, Pune, Kolkata),
10 global suppliers, and 3 product lines (Urban Commuter, Sport, Cargo).
Key cost drivers: BATTERY cells (single-source from Abbott-Munoz, ~40% of BOM), MOTOR assemblies (dual-source),
and ELECTRONICS modules. Abbott-Munoz supplies batteries to ALL 3 product lines across Chennai and Pune."""


@st.cache_data(ttl=3600, show_spinner=False)
def _ai_summary(prompt_text):
    """Call Cortex COMPLETE with error handling. Returns None on any failure."""
    try:
        safe = prompt_text.replace("'", "''")
        result = run_query_df(f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{CORTEX_MODEL}', '{safe}') AS S")
        if not result.empty:
            return result.iloc[0]["S"].strip()
    except Exception:
        return None
    return None


def _filter_context(vendors, plants, cats):
    """Build a short string describing active filters for AI prompt."""
    parts = []
    if vendors:
        parts.append(f"Vendors: {', '.join(vendors)}")
    if plants:
        parts.append(f"Plants: {', '.join(plants)}")
    if cats:
        parts.append(f"Categories: {', '.join(cats)}")
    if not parts:
        return "Filters: All vendors, all plants, all categories."
    return "Active filters: " + "; ".join(parts) + "."


def ai_spend_trend_summary(year, df, filter_str):
    data = ", ".join(f"{r['MONTH']}: ${r['Spend (M)']:.1f}M" for _, r in df.iterrows())
    total = df["Spend (M)"].sum()
    peak = df.loc[df["Spend (M)"].idxmax(), "MONTH"]
    low = df.loc[df["Spend (M)"].idxmin(), "MONTH"]
    prompt = f"""{COCOEV_CONTEXT}
{filter_str}

AP spend trend for FY{year}:
{data}
Total: ${total:.1f}M | Peak: {peak} | Low: {low}

In under 50 words: trend direction, one notable spike/dip cause specific to the filtered scope, and one actionable insight. Reference the filter values. Flowing prose, no bullets."""
    return _ai_summary(prompt)


def ai_vendor_summary(year, df, filter_str):
    top5 = df.head(5)
    data = ", ".join(f"{r['VENDOR_NAME']}: ${r['Spend (M)']:.1f}M" for _, r in top5.iterrows())
    total = df["Spend (M)"].sum()
    prompt = f"""{COCOEV_CONTEXT}
{filter_str}

Top vendors by AP spend in FY{year} (total ${total:.1f}M across {len(df)} vendors):
{data}

In under 50 words: concentration risk specific to the filtered scope, and one recommendation. Reference the filter values. Flowing prose, no bullets."""
    return _ai_summary(prompt)


def ai_region_summary(year, df, filter_str):
    data = ", ".join(f"{r['REGION']}: ${r['Spend (M)']:.1f}M" for _, r in df.iterrows())
    prompt = f"""{COCOEV_CONTEXT}
{filter_str}

Spend by supplier region in FY{year}:
{data}

In under 50 words: geographic risk specific to the filtered scope and one resilience insight. Reference the filter values. Flowing prose, no bullets."""
    return _ai_summary(prompt)


# --- Charts in Tabs ---
yr = selected_year
filter_str = _filter_context(selected_vendors, selected_plants, selected_categories)
tab1, tab2, tab3, tab4, tab5 = st.tabs([f"Spend Trend {yr}", f"Vendors & Pipeline {yr}", f"Spend by Region {yr}", f"Performance KPIs {yr}", "Gap Analysis (Last 90 Days)"])

with tab1:
    df_trend = get_spend_trend(selected_year, vendor_ids, plant_ids, categories)
    if not df_trend.empty:
        df_trend["SPEND"] = pd.to_numeric(df_trend["SPEND"], errors="coerce").fillna(0)
        df_trend["Spend (M)"] = (df_trend["SPEND"] / 1_000_000).round(1)

        line = alt.Chart(df_trend).mark_line(
            color="#E45756", strokeWidth=2.5
        ).encode(
            x=alt.X("MONTH:N", title=f"Month ({yr})", sort=alt.SortField("MONTH_NUM")),
            y=alt.Y("Spend (M):Q", title=f"Spend ($ Millions) — {yr}"),
        )
        points = alt.Chart(df_trend).mark_circle(
            color="#E45756", size=60
        ).encode(
            x=alt.X("MONTH:N", sort=alt.SortField("MONTH_NUM")),
            y="Spend (M):Q",
            tooltip=["MONTH:N", alt.Tooltip("Spend (M):Q", format=".1f")]
        )
        labels = alt.Chart(df_trend).mark_text(
            align="center", dy=-12, fontSize=10, color="#444"
        ).encode(
            x=alt.X("MONTH:N", sort=alt.SortField("MONTH_NUM")),
            y="Spend (M):Q",
            text=alt.Text("Spend (M):Q", format=".1f")
        )
        st.altair_chart(line + points + labels, use_container_width=True)

        summary = ai_spend_trend_summary(selected_year, df_trend, filter_str)
        if summary:
            st.caption(f"**Insight:** {summary}")
    else:
        st.info("No spend data for the selected filters.")

with tab2:
    col_left, col_right = st.columns(2)

    with col_left:
        st.subheader(f"Spend by Vendor (Top 10) — {yr}")
        df_vendor = get_spend_by_vendor(selected_year, vendor_ids, plant_ids, categories)
        if not df_vendor.empty:
            df_vendor["SPEND"] = pd.to_numeric(df_vendor["SPEND"], errors="coerce").fillna(0)
            df_vendor["Spend (M)"] = (df_vendor["SPEND"] / 1_000_000).round(2)

            vendor_chart = alt.Chart(df_vendor).mark_bar(
                cornerRadiusEnd=4, color="#4C78A8"
            ).encode(
                x=alt.X("Spend (M):Q", title=f"Spend ($ Millions) — {yr}"),
                y=alt.Y("VENDOR_NAME:N", sort="-x", title=""),
                color=alt.Color("Spend (M):Q", scale=alt.Scale(scheme="tealblues"), legend=None),
                tooltip=["VENDOR_NAME:N", alt.Tooltip("Spend (M):Q", format=".2f")]
            ).properties(height=350)
            st.altair_chart(vendor_chart, use_container_width=True)
        else:
            st.info("No vendor spend data for the selected filters.")

    with col_right:
        st.subheader(f"Order Pipeline (GR/IR) — {yr}")
        df_pipeline = get_order_pipeline(selected_year, vendor_ids, plant_ids, categories)
        if not df_pipeline.empty:
            df_pipeline["COUNT"] = pd.to_numeric(df_pipeline["COUNT"], errors="coerce").fillna(0).astype(int)

            pipeline_chart = alt.Chart(df_pipeline).mark_line(
                strokeWidth=2.5, point=alt.OverlayMarkDef(size=50)
            ).encode(
                x=alt.X("MONTH:O", title=f"Month ({yr})"),
                y=alt.Y("COUNT:Q", title=f"Count — {yr}"),
                color=alt.Color("EVENT_TYPE:N", scale=alt.Scale(
                    domain=df_pipeline["EVENT_TYPE"].unique().tolist(),
                    range=["#F58518", "#54A24B", "#4C78A8", "#E45756"]
                ), title="Type"),
                tooltip=["MONTH:O", "EVENT_TYPE:N", "COUNT:Q"]
            ).properties(height=350)
            st.altair_chart(pipeline_chart, use_container_width=True)
        else:
            st.info("No pipeline data for the selected filters.")

    # AI summary for vendor tab (below both charts)
    if not df_vendor.empty:
        vendor_summary = ai_vendor_summary(selected_year, df_vendor, filter_str)
        if vendor_summary:
            st.caption(f"**Insight:** {vendor_summary}")

with tab3:
    df_region = get_spend_by_region(selected_year, vendor_ids, plant_ids, categories)
    if not df_region.empty:
        df_region["SPEND"] = pd.to_numeric(df_region["SPEND"], errors="coerce").fillna(0)
        df_region["Spend (M)"] = (df_region["SPEND"] / 1_000_000).round(2)

        region_chart = alt.Chart(df_region).mark_bar(
            cornerRadiusEnd=4
        ).encode(
            x=alt.X("REGION:N", title="", sort="-y"),
            y=alt.Y("Spend (M):Q", title=f"Spend ($ Millions) — {yr}"),
            color=alt.Color("REGION:N", scale=alt.Scale(scheme="tableau10"), legend=None),
            tooltip=["REGION:N", alt.Tooltip("Spend (M):Q", format=".2f")]
        ).properties(height=350)
        st.altair_chart(region_chart, use_container_width=True)

        region_summary = ai_region_summary(selected_year, df_region, filter_str)
        if region_summary:
            st.caption(f"**Insight:** {region_summary}")
    else:
        st.info("No region data for the selected filters.")

with tab4:
    st.caption("Procurement process health indicators calculated from Gold star schema.")

    # KPI 1: Procurement Cycle Time (PO date → GR date)
    df_cycle = run_query_df(f"""
        SELECT AVG(DATEDIFF('day', d_po.FULL_DATE, d_gr.FULL_DATE)) AS AVG_CYCLE_DAYS,
               MEDIAN(DATEDIFF('day', d_po.FULL_DATE, d_gr.FULL_DATE)) AS MEDIAN_CYCLE_DAYS,
               MIN(DATEDIFF('day', d_po.FULL_DATE, d_gr.FULL_DATE)) AS MIN_DAYS,
               MAX(DATEDIFF('day', d_po.FULL_DATE, d_gr.FULL_DATE)) AS MAX_DAYS
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY h
        JOIN SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po ON po.PO_ID = h.PO_ID AND po.PO_LINE = h.PO_LINE
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d_po ON d_po.DATE_KEY = po.DATE_KEY
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d_gr ON d_gr.DATE_KEY = h.DATE_KEY
        WHERE h.EVENT_TYPE = '1' AND h.FISCAL_YEAR = '{selected_year}'
    """)

    # KPI 2: GR Completeness (% PO lines that received goods)
    df_accuracy = run_query_df(f"""
        SELECT
            COUNT(DISTINCT po.PO_ID || '|' || po.PO_LINE) AS TOTAL_PO_LINES,
            COUNT(DISTINCT CASE WHEN h.PO_ID IS NOT NULL THEN po.PO_ID || '|' || po.PO_LINE END) AS RECEIVED_LINES
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        LEFT JOIN (SELECT DISTINCT PO_ID, PO_LINE FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '1' AND FISCAL_YEAR = '{selected_year}') h
            ON h.PO_ID = po.PO_ID AND h.PO_LINE = po.PO_LINE
        WHERE po.FISCAL_YEAR = {selected_year}
    """)

    # KPI 3: Vendor Performance (on-time delivery %)
    df_vendor_perf = run_query_df(f"""
        SELECT
            COUNT(*) AS TOTAL_GR,
            SUM(CASE WHEN DATEDIFF('day', d_po.FULL_DATE, d_gr.FULL_DATE) <= 14 THEN 1 ELSE 0 END) AS ON_TIME
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY h
        JOIN SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po ON po.PO_ID = h.PO_ID AND po.PO_LINE = h.PO_LINE
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d_po ON d_po.DATE_KEY = po.DATE_KEY
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d_gr ON d_gr.DATE_KEY = h.DATE_KEY
        WHERE h.EVENT_TYPE = '1' AND h.FISCAL_YEAR = '{selected_year}'
    """)

    # KPI 5: Invoice vs GR Variance (total invoiced vs total goods received value)
    df_savings = run_query_df(f"""
        SELECT
            COALESCE(SUM(CASE WHEN EVENT_TYPE = '1' THEN AMOUNT_LOCAL_CURRENCY ELSE 0 END), 0) AS TOTAL_GR_VALUE,
            COALESCE(SUM(CASE WHEN EVENT_TYPE = '2' THEN AMOUNT_LOCAL_CURRENCY ELSE 0 END), 0) AS TOTAL_IR_VALUE
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY
        WHERE FISCAL_YEAR = '{selected_year}'
    """)

    # KPI 6: Compliance (3-way match rate)
    df_compliance = run_query_df(f"""
        SELECT
            COUNT(DISTINCT po.PO_ID || po.PO_LINE) AS TOTAL_PO_LINES,
            COUNT(DISTINCT CASE WHEN gr.PO_ID IS NOT NULL AND ir.PO_ID IS NOT NULL THEN po.PO_ID || po.PO_LINE END) AS MATCHED_LINES
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        LEFT JOIN (SELECT DISTINCT PO_ID, PO_LINE FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '1' AND FISCAL_YEAR = '{selected_year}') gr ON gr.PO_ID = po.PO_ID AND gr.PO_LINE = po.PO_LINE
        LEFT JOIN (SELECT DISTINCT PO_ID, PO_LINE FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '2' AND FISCAL_YEAR = '{selected_year}') ir ON ir.PO_ID = po.PO_ID AND ir.PO_LINE = po.PO_LINE
        WHERE po.FISCAL_YEAR = {selected_year}
    """)

    # KPI 7: Data Accuracy (% records with complete key fields)
    df_data_quality = run_query_df(f"""
        SELECT
            COUNT(*) AS TOTAL,
            SUM(CASE WHEN po.VENDOR_SK IS NOT NULL AND po.MATERIAL_SK IS NOT NULL AND po.PLANT_SK IS NOT NULL THEN 1 ELSE 0 END) AS COMPLETE
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        WHERE po.FISCAL_YEAR = {selected_year}
    """)

    # KPI 4: AP Aging Risk (% of open items past due)
    df_payment = run_query_df(f"""
        SELECT
            COUNT(*) AS TOTAL_ITEMS,
            SUM(CASE WHEN d.FULL_DATE <= CURRENT_DATE() THEN 1 ELSE 0 END) AS PAST_DUE,
            COALESCE(SUM(CASE WHEN d.FULL_DATE <= CURRENT_DATE() THEN ap.AMOUNT_LOCAL_CURRENCY ELSE 0 END), 0) AS PAST_DUE_AMOUNT
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_OPEN_ITEMS ap
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = ap.DUE_DATE_KEY
        WHERE ap.FISCAL_YEAR = '{selected_year}'
    """)

    # Display KPIs in 2 rows of 4
    kpi_row1 = st.columns(4)
    with kpi_row1[0]:
        avg_cycle = float(df_cycle.iloc[0]["AVG_CYCLE_DAYS"] or 0) if not df_cycle.empty else 0
        median_cycle = float(df_cycle.iloc[0]["MEDIAN_CYCLE_DAYS"] or 0) if not df_cycle.empty else 0
        st.metric("Procurement Cycle Time", f"{avg_cycle:.0f} days", help="Average days from PO creation to Goods Receipt.")
        st.caption(f"Median: {median_cycle:.0f} days")

    with kpi_row1[1]:
        if not df_accuracy.empty:
            total = int(df_accuracy.iloc[0]["TOTAL_PO_LINES"] or 0)
            received = int(df_accuracy.iloc[0]["RECEIVED_LINES"] or 0)
            pct = (received / total * 100) if total > 0 else 0
        else:
            pct = 0
        st.metric("GR Completeness", f"{pct:.1f}%", help="% of PO lines that have received goods (Goods Receipt posted).")

    with kpi_row1[2]:
        if not df_vendor_perf.empty:
            total_gr = int(df_vendor_perf.iloc[0]["TOTAL_GR"] or 0)
            on_time = int(df_vendor_perf.iloc[0]["ON_TIME"] or 0)
            otd_pct = (on_time / total_gr * 100) if total_gr > 0 else 0
        else:
            otd_pct = 0
        st.metric("Vendor On-Time Delivery", f"{otd_pct:.1f}%", help="% of goods receipts within 14 days of PO date.")

    with kpi_row1[3]:
        if not df_payment.empty:
            total_items = int(df_payment.iloc[0]["TOTAL_ITEMS"] or 0)
            past_due = int(df_payment.iloc[0]["PAST_DUE"] or 0)
            past_due_amt = float(df_payment.iloc[0]["PAST_DUE_AMOUNT"] or 0)
            aging_pct = (past_due / total_items * 100) if total_items > 0 else 0
        else:
            aging_pct = 0
            past_due_amt = 0
        st.metric("AP Aging Risk", f"{aging_pct:.0f}%", help="% of AP open items past their due date. Lower is better.")
        if past_due_amt > 0:
            st.caption(f"${past_due_amt/1e6:.1f}M past due")

    kpi_row2 = st.columns(4)
    with kpi_row2[0]:
        if not df_savings.empty:
            gr_val = float(df_savings.iloc[0]["TOTAL_GR_VALUE"] or 0)
            ir_val = float(df_savings.iloc[0]["TOTAL_IR_VALUE"] or 0)
            variance = gr_val - ir_val
            var_str = f"${abs(variance)/1e6:.1f}M" if abs(variance) >= 1e6 else f"${abs(variance):,.0f}"
        else:
            variance = 0
            var_str = "$0"
        if variance > 0:
            st.metric("Uninvoiced Goods", var_str, help="GR value exceeds IR value — goods received but not yet invoiced. Cash flow risk.")
        else:
            st.metric("Invoice Excess", var_str, help="IR value exceeds GR value — invoiced more than received. Overbilling risk.")

    with kpi_row2[1]:
        if not df_compliance.empty:
            total_lines = int(df_compliance.iloc[0]["TOTAL_PO_LINES"] or 0)
            matched = int(df_compliance.iloc[0]["MATCHED_LINES"] or 0)
            match_pct = (matched / total_lines * 100) if total_lines > 0 else 0
        else:
            match_pct = 0
        st.metric("3-Way Match Rate", f"{match_pct:.1f}%", help="% of PO lines with both Goods Receipt and Invoice Receipt (full compliance).")

    with kpi_row2[2]:
        if not df_data_quality.empty:
            total_dq = int(df_data_quality.iloc[0]["TOTAL"] or 0)
            complete_dq = int(df_data_quality.iloc[0]["COMPLETE"] or 0)
            dq_pct = (complete_dq / total_dq * 100) if total_dq > 0 else 0
        else:
            dq_pct = 0
        st.metric("Data Completeness", f"{dq_pct:.1f}%", help="% of PO records with vendor, material, and plant fields populated.")

    with kpi_row2[3]:
        st.metric("User Satisfaction", "N/A", help="No survey data available in current data model.")
        st.caption("Requires external feedback data.")

with tab5:
    st.caption("Identifies specific procurement process gaps requiring investigation. Use AI Investigation on the Control Tower to get root cause analysis.")
    st.info("Showing gaps from the **last 90 days only** — recent transactions that are actively open and need attention. Historical gaps that have been resolved are excluded.")

    # Gap 1: GR without Invoice (uninvoiced goods) — last 90 days
    df_gr_gap = run_query_df(f"""
        SELECT v.VENDOR_NAME, COUNT(DISTINCT gr.PO_ID || '|' || gr.PO_LINE) AS GAP_LINES,
               COALESCE(SUM(gr.AMOUNT_LOCAL_CURRENCY), 0) AS GAP_AMOUNT
        FROM (SELECT PO_ID, PO_LINE, AMOUNT_LOCAL_CURRENCY, DATE_KEY FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '1') gr
        LEFT JOIN (SELECT DISTINCT PO_ID, PO_LINE FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '2') ir
            ON ir.PO_ID = gr.PO_ID AND ir.PO_LINE = gr.PO_LINE
        JOIN SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po ON po.PO_ID = gr.PO_ID AND po.PO_LINE = gr.PO_LINE
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_VENDOR v ON v.VENDOR_SK = po.VENDOR_SK
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = gr.DATE_KEY
        WHERE ir.PO_ID IS NULL AND d.FULL_DATE >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY v.VENDOR_NAME ORDER BY GAP_AMOUNT DESC LIMIT 10
    """)

    # Gap 2: AP Aging Buckets (inherently current — open items only)
    df_aging = run_query_df(f"""
        SELECT
            CASE
                WHEN DATEDIFF('day', d.FULL_DATE, CURRENT_DATE()) BETWEEN 0 AND 30 THEN '0-30 days'
                WHEN DATEDIFF('day', d.FULL_DATE, CURRENT_DATE()) BETWEEN 31 AND 60 THEN '31-60 days'
                WHEN DATEDIFF('day', d.FULL_DATE, CURRENT_DATE()) BETWEEN 61 AND 90 THEN '61-90 days'
                ELSE '90+ days'
            END AS AGING_BUCKET,
            COUNT(*) AS ITEM_COUNT,
            COALESCE(SUM(ap.AMOUNT_LOCAL_CURRENCY), 0) AS TOTAL_AMOUNT
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_OPEN_ITEMS ap
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = ap.DUE_DATE_KEY
        GROUP BY 1 ORDER BY 1
    """)

    # Gap 3: Vendor Concentration (material groups with few vendors) — last 90 days orders
    df_concentration = run_query_df(f"""
        SELECT m.MATERIAL_GROUP, COUNT(DISTINCT po.VENDOR_SK) AS VENDOR_COUNT,
               SUM(po.GROSS_VALUE) AS TOTAL_VALUE
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_MATERIAL m ON m.MATERIAL_SK = po.MATERIAL_SK
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = po.DATE_KEY
        WHERE d.FULL_DATE >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY m.MATERIAL_GROUP ORDER BY VENDOR_COUNT ASC, TOTAL_VALUE DESC
    """)

    # Gap 4: Stale POs (no GR within 90 days of PO creation)
    df_stale = run_query_df(f"""
        SELECT v.VENDOR_NAME, COUNT(*) AS STALE_LINES, SUM(po.GROSS_VALUE) AS STALE_VALUE
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PURCHASE_ORDERS po
        LEFT JOIN (SELECT DISTINCT PO_ID, PO_LINE FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_PO_HISTORY WHERE EVENT_TYPE = '1') gr
            ON gr.PO_ID = po.PO_ID AND gr.PO_LINE = po.PO_LINE
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_VENDOR v ON v.VENDOR_SK = po.VENDOR_SK
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = po.DATE_KEY
        WHERE gr.PO_ID IS NULL AND d.FULL_DATE >= DATEADD('day', -90, CURRENT_DATE())
        GROUP BY v.VENDOR_NAME ORDER BY STALE_LINES DESC LIMIT 10
    """)

    # Gap 5: Invoice without PO reference — last 90 days
    df_no_po = run_query_df(f"""
        SELECT
            COUNT(*) AS TOTAL_INVOICES,
            SUM(CASE WHEN PO_ID IS NULL THEN 1 ELSE 0 END) AS NO_PO_INVOICES,
            COALESCE(SUM(CASE WHEN PO_ID IS NULL THEN GROSS_INVOICE_AMOUNT ELSE 0 END), 0) AS NO_PO_AMOUNT
        FROM SAP_P2P_FINANCE_DEV.GOLD.FCT_AP_INVOICES i
        JOIN SAP_P2P_FINANCE_DEV.GOLD.DIM_DATE d ON d.DATE_KEY = i.DATE_KEY
        WHERE d.FULL_DATE >= DATEADD('day', -90, CURRENT_DATE())
    """)

    # Display gaps
    gap_col1, gap_col2 = st.columns(2)

    with gap_col1:
        st.markdown("**GR without Invoice (Uninvoiced Goods)**")
        st.caption("Goods received but vendor hasn't invoiced — AP accrual risk.")
        if not df_gr_gap.empty:
            display_gr = df_gr_gap.copy()
            display_gr["GAP_AMOUNT"] = display_gr["GAP_AMOUNT"].apply(lambda x: f"${float(x)/1e6:.1f}M" if float(x) >= 1e6 else f"${float(x):,.0f}")
            display_gr.columns = ["Vendor", "PO Lines", "Uninvoiced Amount"]
            st.dataframe(display_gr, use_container_width=True, hide_index=True)
        else:
            st.success("No GR/IR gaps found.")

    with gap_col2:
        st.markdown("**AP Aging Buckets**")
        st.caption("Open payables grouped by days past due date.")
        if not df_aging.empty:
            display_aging = df_aging.copy()
            display_aging["TOTAL_AMOUNT"] = display_aging["TOTAL_AMOUNT"].apply(lambda x: f"${float(x)/1e6:.1f}M" if float(x) >= 1e6 else f"${float(x):,.0f}")
            display_aging.columns = ["Aging Bucket", "Items", "Amount"]
            st.dataframe(display_aging, use_container_width=True, hide_index=True)
        else:
            st.success("No aging items.")

    st.divider()
    gap_col3, gap_col4 = st.columns(2)

    with gap_col3:
        st.markdown("**Vendor Concentration Risk**")
        st.caption("Material groups and number of qualifying vendors — fewer = higher risk.")
        if not df_concentration.empty:
            display_conc = df_concentration.copy()
            display_conc["TOTAL_VALUE"] = display_conc["TOTAL_VALUE"].apply(lambda x: f"${float(x)/1e6:.1f}M" if float(x) >= 1e6 else f"${float(x):,.0f}")
            display_conc.columns = ["Material Group", "Vendors", "Spend"]
            st.dataframe(display_conc, use_container_width=True, hide_index=True)
        else:
            st.info("No concentration data.")

    with gap_col4:
        st.markdown("**Stale Purchase Orders**")
        st.caption("PO lines with no Goods Receipt — orders placed but never fulfilled.")
        if not df_stale.empty:
            display_stale = df_stale.copy()
            display_stale["STALE_VALUE"] = display_stale["STALE_VALUE"].apply(lambda x: f"${float(x):,.0f}")
            display_stale.columns = ["Vendor", "Stale Lines", "Value"]
            st.dataframe(display_stale, use_container_width=True, hide_index=True)
        else:
            st.success("No stale POs found.")

    st.divider()
    st.markdown("**Invoice-PO Linkage Gap**")
    st.caption("Invoices without a PO reference — potential maverick spend or data quality issue.")
    if not df_no_po.empty:
        total_inv = int(df_no_po.iloc[0]["TOTAL_INVOICES"] or 0)
        no_po_inv = int(df_no_po.iloc[0]["NO_PO_INVOICES"] or 0)
        no_po_amt = float(df_no_po.iloc[0]["NO_PO_AMOUNT"] or 0)
        no_po_pct = (no_po_inv / total_inv * 100) if total_inv > 0 else 0
        link_cols = st.columns(3)
        with link_cols[0]:
            st.metric("Total Invoices", f"{total_inv:,}")
        with link_cols[1]:
            st.metric("Without PO Reference", f"{no_po_inv:,} ({no_po_pct:.0f}%)")
        with link_cols[2]:
            amt_str = f"${no_po_amt/1e6:.0f}M" if no_po_amt >= 1e6 else f"${no_po_amt:,.0f}"
            st.metric("Unlinked Amount", amt_str)

# ============================================================
# SECTION: ASK ABOUT THIS DATA
# ============================================================

st.divider()

# Context-aware question suggestions based on selected topic
ASK_THEMES = {
    "Spend Trend": {
        "title": "Ask About Spend Trends",
        "questions": [
            ("📊", "Which month had the highest spend in the chart above?"),
            ("📊", "Is the spend trend going up or down this year?"),
            ("🔍", "What is the total spend by month for 2025?"),
            ("🔍", "Which vendor has the highest total PO value?"),
        ]
    },
    "Vendors & Pipeline": {
        "title": "Ask About Vendor Performance",
        "questions": [
            ("📊", "Which vendor has the highest spend in the chart?"),
            ("📊", "How many vendors are shown in the top spend list?"),
            ("🔍", "Show top 10 vendors by total invoice amount"),
            ("🔍", "Which vendors supply to more than one plant?"),
        ]
    },
    "Spend by Region": {
        "title": "Ask About Regional Spend",
        "questions": [
            ("📊", "Which region has the highest spend shown above?"),
            ("📊", "How is spend split across the regions in the chart?"),
            ("🔍", "What is total PO value by plant name?"),
            ("🔍", "Show spend by material group for top 10 groups"),
        ]
    },
    "Performance KPIs": {
        "title": "Ask About KPI Performance",
        "questions": [
            ("📊", "What is the total PO value shown in the KPIs?"),
            ("📊", "How many invoices are currently in the system?"),
            ("🔍", "What is the average invoice amount by vendor?"),
            ("🔍", "Show total GR value vs total IR value by vendor"),
        ]
    },
    "Gap Analysis": {
        "title": "Ask About Gaps & Risks",
        "questions": [
            ("📊", "What is the total gap amount shown in the analysis?"),
            ("📊", "How many vendors have a GR/IR mismatch above?"),
            ("🔍", "Which vendors have AP open items over 100000?"),
            ("🔍", "What is total open payable amount by vendor?"),
        ]
    },
}

ask_context = st.selectbox("Select context", list(ASK_THEMES.keys()), index=0, label_visibility="collapsed")
theme = ASK_THEMES[ask_context]
st.subheader(theme["title"])
st.markdown('<span style="font-size:12px;color:#6b7280;"><span style="color:#2563eb;font-weight:600;">Blue</span> = Answer from visuals above &nbsp;&nbsp; <span style="color:#7c3aed;font-weight:600;">Purple</span> = Query the data warehouse</span>', unsafe_allow_html=True)

# Show clickable sample questions with color styling
visual_qs = [(i, q) for i, (icon, q) in enumerate(theme["questions"]) if icon == "📊"]
data_qs = [(i, q) for i, (icon, q) in enumerate(theme["questions"]) if icon == "🔍"]

q_col1, q_col2 = st.columns(2)
with q_col1:
    for i, q in visual_qs:
        st.markdown(f'<p style="font-size:13px;color:#2563eb;margin-bottom:4px;font-weight:500;">From Visuals:</p>', unsafe_allow_html=True)
        if st.button(q, key=f"dash_q_{i}", use_container_width=True):
            st.session_state["dash_prefill"] = q
with q_col2:
    for i, q in data_qs:
        st.markdown(f'<p style="font-size:13px;color:#7c3aed;margin-bottom:4px;font-weight:500;">From Data Warehouse:</p>', unsafe_allow_html=True)
        if st.button(q, key=f"dash_q_{i}", use_container_width=True):
            st.session_state["dash_prefill"] = q

SYSTEM_PROMPT = """You are a procurement analytics assistant for an Indian electric scooter manufacturer.
You ONLY answer questions related to procurement, supply chain, invoices, purchase orders, vendors,
spend analysis, payment terms, goods receipts, AP (Accounts Payable), supplier risk, and manufacturing materials.

GUARDRAIL: If the user asks about anything unrelated to procurement or supply chain business processes
(e.g., weather, sports, coding, general knowledge, politics, personal advice), respond ONLY with:
"I can only answer questions related to procurement and supply chain. Please ask about spend, vendors, invoices, POs, or supplier risk."

You have access to the following data context from the current dashboard view:"""

if "dash_chat_history" not in st.session_state:
    st.session_state["dash_chat_history"] = []

# Build data context from current charts
data_context_parts = []
try:
    if not df_trend.empty:
        trend_str = ", ".join(f"{r['MONTH']}: ${r['Spend (M)']:.1f}M" for _, r in df_trend.iterrows())
        data_context_parts.append(f"Spend Trend FY{selected_year}: {trend_str}")
except Exception:
    pass
try:
    if not df_vendor.empty:
        vendor_str = ", ".join(f"{r['VENDOR_NAME']}: ${r['Spend (M)']:.1f}M" for _, r in df_vendor.head(5).iterrows())
        data_context_parts.append(f"Top Vendors FY{selected_year}: {vendor_str}")
except Exception:
    pass
try:
    if not df_region.empty:
        region_str = ", ".join(f"{r['REGION']}: ${r['Spend (M)']:.1f}M" for _, r in df_region.iterrows())
        data_context_parts.append(f"Spend by Region FY{selected_year}: {region_str}")
except Exception:
    pass

data_context = "\n".join(data_context_parts) if data_context_parts else "No chart data loaded yet."
full_context = f"{SYSTEM_PROMPT}\n{filter_str}\n{data_context}"

# Rotating snowflake for assistant responses
st.markdown("""
<style>
[data-testid="chatAvatarIcon-assistant"] {
    animation: spin 3s linear infinite;
}
@keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}
.stChatMessage [data-testid="stMarkdownContainer"] {
    font-size: 14px;
    line-height: 1.6;
}
</style>
""", unsafe_allow_html=True)

# Display chat history
for msg in st.session_state["dash_chat_history"]:
    avatar = "❄️" if msg["role"] == "assistant" else None
    with st.chat_message(msg["role"], avatar=avatar):
        st.markdown(msg["content"])

# Chat input
user_q = st.chat_input("Ask about the procurement data shown above...")

# Handle prefill from sample question buttons
if "dash_prefill" in st.session_state and st.session_state["dash_prefill"]:
    user_q = st.session_state.pop("dash_prefill")

if user_q:
    st.session_state["dash_chat_history"].append({"role": "user", "content": user_q})
    with st.chat_message("user"):
        st.markdown(user_q)

    with st.chat_message("assistant", avatar="❄️"):
        with st.spinner("Analyzing..."):
            try:
                # First try: answer from dashboard context
                logger.info(f"User question: {user_q}")
                prompt = f"{full_context}\n\nUser question: {user_q}\n\nIf you can answer from the data context above, answer in under 80 words. If the data context does NOT contain enough information to answer, respond ONLY with the exact text: NEED_SQL_QUERY. If the question is not about procurement, respond with the guardrail message."
                safe = prompt.replace("'", "''")
                logger.debug(f"Context check prompt sent to {CORTEX_MODEL}")
                result = run_query_df(f"SELECT SNOWFLAKE.CORTEX.COMPLETE('{CORTEX_MODEL}', '{safe}') AS S")
                answer = ""
                if not result.empty:
                    answer = result.iloc[0]["S"].strip()
                logger.info(f"Context check response: {answer[:200]}")

                # If LLM says it needs SQL, fall back to Cortex Complete SQL generation
                if "NEED_SQL_QUERY" in answer or not answer:
                    logger.info("Context insufficient, falling back to SQL generation via mistral-large2")
                    sql_prompt = (
                        "You are a procurement data analyst for CoCoEV, an electric scooter manufacturer. "
                        "Write a Snowflake SQL query to answer the question below.\\n\\n"
                        "TABLES in SAP_P2P_FINANCE_DEV.GOLD schema:\\n"
                        "- FCT_PURCHASE_ORDERS: PO_ID, PO_LINE, VENDOR_SK, MATERIAL_SK, PLANT_SK, NET_VALUE (NUMBER), GROSS_VALUE (NUMBER), QUANTITY (NUMBER), FISCAL_YEAR (NUMBER), DATE_KEY\\n"
                        "- FCT_AP_INVOICES: INVOICE_ID, VENDOR_SK, GROSS_INVOICE_AMOUNT (NUMBER), FISCAL_YEAR (VARCHAR), DATE_KEY\\n"
                        "- FCT_AP_OPEN_ITEMS: COMPANY_CODE, DOC_ID, VENDOR_SK, AMOUNT_LOCAL_CURRENCY (NUMBER), DUE_DATE_KEY\\n"
                        "- FCT_PO_HISTORY: PO_ID, PO_LINE, EVENT_TYPE (1=GR, 2=IR), AMOUNT_LOCAL_CURRENCY (NUMBER), DATE_KEY\\n"
                        "- FCT_GOODS_MOVEMENTS: MATERIAL_DOC, MATERIAL_SK, PLANT_SK, QUANTITY (NUMBER), AMOUNT_LOCAL_CURRENCY (NUMBER), DATE_KEY\\n"
                        "- DIM_VENDOR: VENDOR_SK, VENDOR_NAME, VENDOR_ID, COUNTRY\\n"
                        "- DIM_MATERIAL: MATERIAL_SK, MATERIAL_DESCRIPTION, MATERIAL_TYPE, MATERIAL_GROUP\\n"
                        "- DIM_PLANT: PLANT_SK, PLANT_NAME, PLANT_ID\\n"
                        "- DIM_DATE: DATE_KEY, FULL_DATE, YEAR, MONTH, MONTH_NAME\\n\\n"
                        "RULES:\\n"
                        "- Always use fully qualified table names (SAP_P2P_FINANCE_DEV.GOLD.table_name)\\n"
                        "- Join facts to dimensions using SK columns (e.g. FCT_PURCHASE_ORDERS.VENDOR_SK = DIM_VENDOR.VENDOR_SK)\\n"
                        "- Every non-aggregated column in SELECT must appear in GROUP BY\\n"
                        "- Do NOT use window functions (LAG, LEAD, ROW_NUMBER) combined with GROUP BY in the same query level. Use a CTE or subquery if needed.\\n"
                        "- PARTITION BY columns must also be in GROUP BY if aggregating\\n"
                        "- Use DIM_DATE joined on DATE_KEY for month/year breakdowns\\n"
                        "- Keep queries simple: prefer SUM, COUNT, AVG with GROUP BY\\n"
                        "- LIMIT results to 20 rows\\n\\n"
                        f"Question: {user_q}\\n\\n"
                        "Return ONLY the SQL query. No markdown, no explanation, no backticks, no comments."
                    ).replace("'", "''")

                    sql_result = run_query_df(f"SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-large2', '{sql_prompt}') AS RESPONSE")
                    if not sql_result.empty:
                        generated_sql = sql_result.iloc[0]["RESPONSE"].strip().strip('`').strip()
                        if generated_sql.startswith("sql"):
                            generated_sql = generated_sql[3:].strip()
                        # Remove markdown fences if present
                        if generated_sql.startswith("```"):
                            generated_sql = generated_sql.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                        logger.info(f"Generated SQL: {generated_sql[:500]}")

                        # Execute the generated SQL with timeout
                        try:
                            # Set statement timeout to 20 seconds
                            run_query_df("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 20")
                            data_df = run_query_df(generated_sql)
                            logger.info(f"SQL execution returned {len(data_df)} rows")

                            if not data_df.empty:
                                # Generate a natural language summary of the results
                                summary_prompt = f"Summarize this data in 1-2 sentences for a procurement manager. Be specific with numbers. Data: {data_df.head(10).to_string()}. Question was: {user_q}".replace("'", "''")
                                summary_result = run_query_df(f"SELECT SNOWFLAKE.CORTEX.COMPLETE('mistral-large2', '{summary_prompt}') AS S")
                                summary = summary_result.iloc[0]["S"].strip() if not summary_result.empty else "Here are the results:"
                                st.markdown(summary)
                                st.dataframe(data_df, use_container_width=True, hide_index=True)
                                with st.expander("View SQL", expanded=False):
                                    st.code(generated_sql, language="sql")
                                st.session_state["dash_chat_history"].append({"role": "assistant", "content": summary})
                            else:
                                st.markdown("Query returned no results for this question.")
                                with st.expander("View SQL", expanded=False):
                                    st.code(generated_sql, language="sql")
                                st.session_state["dash_chat_history"].append({"role": "assistant", "content": "Query returned no results."})
                        except Exception as sql_err:
                            logger.error(f"SQL execution failed: {sql_err}")
                            st.warning(f"Generated SQL had an error. Retrying with correction...")
                            # Show the failed SQL for debugging
                            with st.expander("Failed SQL (for debugging)", expanded=False):
                                st.code(generated_sql, language="sql")
                                st.caption(f"Error: {str(sql_err)[:200]}")
                            st.session_state["dash_chat_history"].append({"role": "assistant", "content": f"SQL error: {str(sql_err)[:100]}"})
                    else:
                        st.warning("Could not generate a response.")
                        st.session_state["dash_chat_history"].append({"role": "assistant", "content": "No response."})
                else:
                    st.markdown(answer)
                    st.session_state["dash_chat_history"].append({"role": "assistant", "content": answer})
            except Exception as e:
                logger.error(f"Chat failed: {e}", exc_info=True)
                st.error(f"Failed: {str(e)[:200]}")
                st.session_state["dash_chat_history"].append({"role": "assistant", "content": f"Error: {str(e)[:100]}"})

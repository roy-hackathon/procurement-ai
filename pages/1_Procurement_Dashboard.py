import streamlit as st
import plotly.express as px
from decimal import Decimal
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.snowflake_connection import is_connected
from services.queries import (
    get_filter_options, get_kpi_metrics, get_spend_trend,
    get_spend_by_vendor, get_order_pipeline, get_spend_by_region
)
from config.settings import FISCAL_YEARS, DEFAULT_YEAR, APP_VERSION

st.set_page_config(page_title=f"Procurement Dashboard v{APP_VERSION}", layout="wide", initial_sidebar_state="collapsed")

# Borders around cards and charts
st.markdown("""<style>
[data-testid="stMetric"] {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 12px 16px;
}
[data-testid="stPlotlyChart"] {
    border: 1px solid #e0e0e0;
    border-radius: 8px;
    padding: 8px;
}
</style>""", unsafe_allow_html=True)

st.title(f"Procurement Dashboard v{APP_VERSION}")

if not is_connected():
    st.error("Snowflake connection unavailable. Check credentials in secrets.")
    conn_error = st.session_state.get("conn_error")
    conn_traceback = st.session_state.get("conn_traceback")
    if conn_error:
        st.code(conn_error)
    if conn_traceback:
        with st.expander("Full traceback"):
            st.code(conn_traceback)
    st.stop()


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
    """Calculate YoY percentage change."""
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

st.divider()

# --- KPI Cards with YoY trend ---
metrics = get_kpi_metrics(selected_year, vendor_ids, plant_ids, categories)
prev_year = selected_year - 1
metrics_prev = get_kpi_metrics(prev_year, vendor_ids, plant_ids, categories)

kpi_cols = st.columns(6)
with kpi_cols[0]:
    st.metric("Total Spend", fmt_money(metrics["total_spend"]),
              delta=calc_delta(metrics["total_spend"], metrics_prev["total_spend"]))
with kpi_cols[1]:
    st.metric("Invoices", f"{int(metrics['invoice_count']):,}",
              delta=calc_delta(metrics["invoice_count"], metrics_prev["invoice_count"]))
with kpi_cols[2]:
    st.metric("Purchase Orders", f"{int(metrics['po_count']):,}",
              delta=calc_delta(metrics["po_count"], metrics_prev["po_count"]))
with kpi_cols[3]:
    st.metric("Active Vendors", f"{int(metrics['vendor_count']):,}")
with kpi_cols[4]:
    st.metric("PO Gross Value", fmt_money(metrics["po_gross"]),
              delta=calc_delta(metrics["po_gross"], metrics_prev["po_gross"]))
with kpi_cols[5]:
    st.metric("Avg Invoice", fmt_money(metrics["avg_invoice"]),
              delta=calc_delta(metrics["avg_invoice"], metrics_prev["avg_invoice"]))

st.divider()

# --- Spend Trend ---
st.subheader("Spend Trend")
df_trend = get_spend_trend(selected_year, vendor_ids, plant_ids, categories)
if not df_trend.empty:
    df_trend["SPEND"] = df_trend["SPEND"].apply(to_float)
    fig_trend = px.line(df_trend, x="MONTH", y="SPEND", markers=True,
                        labels={"MONTH": "Month", "SPEND": "Spend ($)"})
    fig_trend.update_layout(yaxis_tickformat="$,.0s", height=350)
    st.plotly_chart(fig_trend, use_container_width=True)
else:
    st.info("No spend data for the selected filters.")

# --- Spend by Vendor + Order Pipeline ---
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("Spend by Vendor (Top 10)")
    df_vendor = get_spend_by_vendor(selected_year, vendor_ids, plant_ids, categories)
    if not df_vendor.empty:
        df_vendor["SPEND"] = df_vendor["SPEND"].apply(to_float)
        fig_vendor = px.bar(df_vendor, x="SPEND", y="VENDOR_NAME", orientation="h",
                            labels={"SPEND": "Spend ($)", "VENDOR_NAME": "Vendor"})
        fig_vendor.update_layout(yaxis=dict(autorange="reversed"), xaxis_tickformat="$,.0s", height=400)
        st.plotly_chart(fig_vendor, use_container_width=True)
    else:
        st.info("No vendor spend data for the selected filters.")

with col_right:
    st.subheader("Order Pipeline (GR/IR)")
    df_pipeline = get_order_pipeline(selected_year, vendor_ids, plant_ids, categories)
    if not df_pipeline.empty:
        df_pipeline["COUNT"] = df_pipeline["COUNT"].apply(int)
        fig_pipeline = px.bar(df_pipeline, x="MONTH", y="COUNT", color="EVENT_TYPE",
                              barmode="group",
                              labels={"MONTH": "Month", "COUNT": "Events", "EVENT_TYPE": "Type"})
        fig_pipeline.update_layout(height=400)
        st.plotly_chart(fig_pipeline, use_container_width=True)
    else:
        st.info("No pipeline data for the selected filters.")

# --- Spend by Region ---
st.subheader("Spend by Region")
df_region = get_spend_by_region(selected_year, vendor_ids, plant_ids, categories)
if not df_region.empty:
    df_region["SPEND"] = df_region["SPEND"].apply(to_float)
    fig_region = px.bar(df_region, x="REGION", y="SPEND",
                        labels={"REGION": "Country", "SPEND": "Spend ($)"})
    fig_region.update_layout(xaxis_tickangle=-45, yaxis_tickformat="$,.0s", height=350)
    st.plotly_chart(fig_region, use_container_width=True)
else:
    st.info("No region data for the selected filters.")

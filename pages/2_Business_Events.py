import streamlit as st
import pandas as pd
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import get_snowflake_connection, is_connected, run_query_df

st.set_page_config(page_title="Business Events", layout="wide", initial_sidebar_state="collapsed")
st.title("Business Events Monitor")
st.caption("All detected anomalies from the AI Event Detection pipeline")

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

DATABASE = "SAP_P2P_FINANCE_DEV"

# Summary stats
stats = run_query_df(f"""
    SELECT
        COUNT(*) AS TOTAL_EVENTS,
        SUM(CASE WHEN STATUS = 'open' THEN 1 ELSE 0 END) AS OPEN,
        SUM(CASE WHEN STATUS = 'investigating' THEN 1 ELSE 0 END) AS INVESTIGATING,
        SUM(CASE WHEN STATUS = 'planned' THEN 1 ELSE 0 END) AS PLANNED,
        SUM(CASE WHEN SEVERITY = 'CRITICAL' THEN 1 ELSE 0 END) AS CRITICAL,
        SUM(CASE WHEN SEVERITY = 'HIGH' THEN 1 ELSE 0 END) AS HIGH,
        COALESCE(SUM(ABS(IMPACT_USD)), 0) AS TOTAL_EXPOSURE
    FROM {DATABASE}.ACTION.BUSINESS_EVENT
    WHERE STATUS NOT IN ('resolved', 'suppressed')
""")

if not stats.empty:
    row = stats.iloc[0]
    cols = st.columns(7)
    with cols[0]:
        st.metric("Total Events", int(row.get("TOTAL_EVENTS") or 0))
    with cols[1]:
        st.metric("Open", int(row.get("OPEN") or 0))
    with cols[2]:
        st.metric("Investigating", int(row.get("INVESTIGATING") or 0))
    with cols[3]:
        st.metric("Planned", int(row.get("PLANNED") or 0))
    with cols[4]:
        st.metric("Critical", int(row.get("CRITICAL") or 0))
    with cols[5]:
        st.metric("High", int(row.get("HIGH") or 0))
    with cols[6]:
        exp = float(row.get("TOTAL_EXPOSURE") or 0)
        st.metric("Exposure", f"${exp/1e6:.1f}M" if exp >= 1e6 else f"${exp:,.0f}")

st.divider()

# Filter
filter_cols = st.columns(3)
with filter_cols[0]:
    severity_filter = st.multiselect("Severity", ["CRITICAL", "HIGH", "MEDIUM", "LOW"], default=None, placeholder="All")
with filter_cols[1]:
    status_filter = st.multiselect("Status", ["open", "investigating", "risk_assessed", "planned"], default=None, placeholder="All")
with filter_cols[2]:
    type_filter = st.multiselect("Event Type", [], default=None, placeholder="All")

# Build query
where_parts = ["STATUS NOT IN ('resolved', 'suppressed')"]
if severity_filter:
    sev_str = ",".join(f"'{s}'" for s in severity_filter)
    where_parts.append(f"SEVERITY IN ({sev_str})")
if status_filter:
    stat_str = ",".join(f"'{s}'" for s in status_filter)
    where_parts.append(f"STATUS IN ({stat_str})")

where_clause = " AND ".join(where_parts)

df_events = run_query_df(f"""
    SELECT EVENT_ID, EVENT_TYPE, ENTITY_KEY, SEVERITY, STATUS,
           IMPACT_USD, HEADLINE, DETECTED_AT, LAST_SEEN_AT, SEEN_COUNT
    FROM {DATABASE}.ACTION.BUSINESS_EVENT
    WHERE {where_clause}
    ORDER BY ABS(IMPACT_USD) DESC NULLS LAST
    LIMIT 200
""")

if not df_events.empty:
    if "IMPACT_USD" in df_events.columns:
        df_events["IMPACT_USD"] = df_events["IMPACT_USD"].apply(lambda x: f"${float(x or 0):,.0f}")
    if "DETECTED_AT" in df_events.columns:
        df_events["DETECTED_AT"] = pd.to_datetime(df_events["DETECTED_AT"]).dt.strftime("%Y-%m-%d %H:%M")
    st.dataframe(df_events, use_container_width=True, hide_index=True, height=500)
else:
    st.info("No business events found. Run the Full Pipeline (page 4) to detect anomalies.")

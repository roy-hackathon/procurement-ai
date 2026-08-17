import streamlit as st
import pandas as pd
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import get_snowflake_connection, is_connected
from services.pipeline import (
    phase_detect, phase_investigate, phase_risk, phase_plan, phase_report, next_run_id, DATABASE
)

st.set_page_config(page_title="Full Pipeline", layout="wide", initial_sidebar_state="collapsed")
st.title("AI Event Detection Pipeline")
st.caption("Detect - Investigate - Assess Risk - Plan Actions - Report")

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

conn = get_snowflake_connection()

# --- Controls ---
col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    limit = st.slider("Max events per phase", 5, 100, 20)
with col2:
    st.write("")
with col3:
    run_pipeline = st.button("Run Full Pipeline", type="primary", use_container_width=True)

st.divider()

if run_pipeline:
    run_id = next_run_id()
    results = {}

    # Phase 1
    with st.status("Phase 1: Detection", expanded=True) as s1:
        st.write("Scanning all active detector views...")
        r1 = phase_detect(conn, run_id, progress_callback=lambda msg: st.write(msg))
        results["detect"] = r1
        s1.update(label=f"Phase 1: {r1['inserted']} new events, {r1['refreshed']} updated ({r1['detectors']} detectors)", state="complete")

    # Phase 2
    with st.status("Phase 2: Investigation", expanded=True) as s2:
        st.write("Classifying root causes with Five-Why logic...")
        r2 = phase_investigate(conn, run_id, limit, progress_callback=lambda msg: st.write(msg))
        results["investigate"] = r2
        branches = {}
        for r in r2:
            branches.setdefault(r.get("branch", "?"), []).append(r)
        s2.update(label=f"Phase 2: {len(r2)} events investigated, {len(branches)} root cause branches", state="complete")

    # Phase 3
    with st.status("Phase 3: Risk Assessment", expanded=True) as s3:
        st.write("Scoring risks and predicting cascade...")
        r3 = phase_risk(conn, run_id, limit, progress_callback=lambda msg: st.write(msg))
        results["risk"] = r3
        by_priority = {}
        for r in r3:
            by_priority.setdefault(r.get("priority", "P4"), []).append(r)
        s3.update(label=f"Phase 3: {len(r3)} risks scored — P1:{len(by_priority.get('P1',[]))} P2:{len(by_priority.get('P2',[]))} P3:{len(by_priority.get('P3',[]))} P4:{len(by_priority.get('P4',[]))}", state="complete")

    # Phase 4
    with st.status("Phase 4: Action Planning", expanded=True) as s4:
        st.write("Selecting actions from catalog, assigning to personas...")
        r4 = phase_plan(conn, run_id, limit, progress_callback=lambda msg: st.write(msg))
        results["plan"] = r4
        s4.update(label=f"Phase 4: {len(r4)} action plans created", state="complete")

    # Phase 5
    with st.status("Phase 5: Report Generation", expanded=True) as s5:
        st.write("Assembling findings from ACTION tables...")
        report_data = phase_report(conn, run_id)
        results["report"] = report_data
        s5.update(label=f"Phase 5: Report assembled ({len(report_data['events'])} events, {len(report_data['actions'])} actions)", state="complete")

    st.success(f"Pipeline complete: {run_id}")
    st.session_state["last_run_id"] = run_id
    st.session_state["last_report"] = report_data

st.divider()

# --- Display Results ---
report_data = st.session_state.get("last_report")
if report_data:
    run_id = report_data["run_id"]
    st.subheader(f"Results: {run_id}")

    # Summary KPIs
    events = report_data.get("events", [])
    actions = report_data.get("actions", [])
    personas = report_data.get("personas", [])

    total_exposure = sum(float(e.get("IMPACT_USD") or 0) for e in events)
    priorities = [e.get("PRIORITY") for e in events if e.get("PRIORITY")]
    p1_count = priorities.count("P1")
    p2_count = priorities.count("P2")
    awaiting = sum(1 for a in actions if a.get("STATUS") == "awaiting_approval")

    kpi_cols = st.columns(6)
    with kpi_cols[0]:
        st.metric("Events Detected", len(events))
    with kpi_cols[1]:
        st.metric("P1 (Critical)", p1_count)
    with kpi_cols[2]:
        st.metric("P2 (High)", p2_count)
    with kpi_cols[3]:
        exp_str = f"${total_exposure/1e6:.1f}M" if total_exposure >= 1e6 else f"${total_exposure:,.0f}"
        st.metric("Total Exposure", exp_str)
    with kpi_cols[4]:
        st.metric("Actions Planned", len(actions))
    with kpi_cols[5]:
        st.metric("Awaiting Approval", awaiting)

    st.divider()

    # --- Persona-Specific Actions ---
    st.subheader("Actions by Persona")

    # Group actions by persona
    persona_actions = {}
    for a in actions:
        owner = a.get("OWNER_PERSONA", "unassigned")
        persona_actions.setdefault(owner, []).append(a)

    # Create persona lookup
    persona_lookup = {p["PERSONA_CODE"]: p for p in personas}

    for persona_code, persona_action_list in sorted(persona_actions.items()):
        persona_info = persona_lookup.get(persona_code, {})
        display_name = persona_info.get("DISPLAY_NAME", persona_code)
        business_role = persona_info.get("BUSINESS_ROLE", "")
        tier = persona_info.get("ESCALATION_TIER", "?")

        with st.expander(f"{display_name} (Tier {tier}) — {len(persona_action_list)} actions", expanded=(tier == 1)):
            if business_role:
                st.caption(business_role[:120])

            df_actions = pd.DataFrame(persona_action_list)
            display_cols = ["ACTION_TYPE", "STATUS", "AUTONOMY_LEVEL", "PRIORITY", "HEADLINE", "IMPACT_USD"]
            available_cols = [c for c in display_cols if c in df_actions.columns]
            if available_cols:
                df_display = df_actions[available_cols].copy()
                if "IMPACT_USD" in df_display.columns:
                    df_display["IMPACT_USD"] = df_display["IMPACT_USD"].apply(lambda x: f"${float(x or 0):,.0f}")
                st.dataframe(df_display, use_container_width=True, hide_index=True)

            # Show approval items
            approval_items = [a for a in persona_action_list if a.get("STATUS") == "awaiting_approval"]
            if approval_items:
                st.warning(f"{len(approval_items)} action(s) require your approval before execution")

    st.divider()

    # --- AI Reasoning (Cortex Agent) ---
    st.subheader("AI Reasoning (Five-Why Analysis)")

    # Build a prompt from the top findings
    top_events = sorted(events, key=lambda e: float(e.get("IMPACT_USD") or 0), reverse=True)[:5]
    if top_events:
        summary_lines = []
        for e in top_events:
            summary_lines.append(f"- {e.get('EVENT_TYPE')}: {e.get('HEADLINE', '')} (${float(e.get('IMPACT_USD') or 0):,.0f}, {e.get('PRIORITY', 'P3')}, owner: {e.get('RECOMMENDED_OWNER', '?')})")
        findings_summary = "\n".join(summary_lines)

        agent_prompt = (
            f"The pipeline just detected these top anomalies:\n{findings_summary}\n\n"
            f"For the highest-impact event, perform a Five-Why investigation. "
            f"Query the GOLD tables to gather evidence across vendor, PO, GR/IR, material, plant, and AP. "
            f"Produce ranked root cause candidates with confidence %, evidence graph, cascade prediction, "
            f"and specific action recommendations for the assigned persona."
        )

        if st.button("Run AI Five-Why Analysis", type="secondary"):
            AGENT_FQN = "SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT"
            request_body = json.dumps({"messages": [{"role": "user", "content": [{"type": "text", "text": agent_prompt}]}]})
            escaped = request_body.replace("'", "''")

            with st.status("Cortex Agent is reasoning...", expanded=True) as agent_status:
                st.write("Invoking Five-Why investigation with evidence traversal...")
                try:
                    cur = conn.cursor()
                    cur.execute(f"SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN('{AGENT_FQN}', '{escaped}')")
                    result = cur.fetchone()
                    cur.close()

                    if result and result[0]:
                        response = json.loads(result[0])
                        agent_status.update(label="AI Reasoning complete", state="complete")

                        # Extract and display
                        for block in response.get("content", []):
                            if block.get("type") == "thinking":
                                with st.expander("Agent Internal Reasoning", expanded=False):
                                    st.text(block.get("text", ""))
                            elif block.get("type") == "text":
                                st.markdown(block.get("text", ""))
                            elif block.get("type") == "suggested_queries":
                                queries = block.get("suggested_queries", block if isinstance(block, list) else [])
                                if queries:
                                    st.caption("Suggested follow-ups:")
                                    for q in queries:
                                        if isinstance(q, dict):
                                            st.code(q.get("query", ""), language=None)
                    else:
                        agent_status.update(label="No response from agent", state="error")
                except Exception as e:
                    agent_status.update(label=f"Agent failed: {e}", state="error")
                    st.error(str(e))

    st.divider()

    # --- Events Table ---
    st.subheader("Investigated Events")
    if events:
        df_events = pd.DataFrame(events)
        display_event_cols = ["EVENT_TYPE", "ENTITY_KEY", "SEVERITY", "IMPACT_USD", "HEADLINE", "PRIORITY", "RECOMMENDED_OWNER", "ROOT_CAUSE_BRANCH"]
        available_event_cols = [c for c in display_event_cols if c in df_events.columns]
        if available_event_cols:
            df_ev = df_events[available_event_cols].copy()
            if "IMPACT_USD" in df_ev.columns:
                df_ev["IMPACT_USD"] = df_ev["IMPACT_USD"].apply(lambda x: f"${float(x or 0):,.0f}")
            st.dataframe(df_ev, use_container_width=True, hide_index=True)

else:
    st.info("Click 'Run Full Pipeline' to execute the 5-phase anomaly detection and action planning pipeline.")

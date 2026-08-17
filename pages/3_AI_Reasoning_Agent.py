import streamlit as st
import json
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import get_snowflake_connection, is_connected, run_query

st.set_page_config(page_title="AI Reasoning Agent", layout="wide", initial_sidebar_state="collapsed")
st.title("AI Procurement Reasoning Agent")
st.caption("Powered by Snowflake Cortex Agent — autonomous Five-Why investigation")

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

AGENT_FQN = "SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT"

# Predefined investigation prompts
QUICK_PROMPTS = {
    "Run Detection Scan": "Run all detector views (VW_DETECT_INVOICE_OVER_PO, VW_DETECT_GR_IR_AGING, VW_DETECT_AP_AGING, VW_DETECT_DUPLICATE_INVOICE) and summarize what anomalies exist. Show top 10 by dollar impact.",
    "Top Vendor Risk": "Identify the top 3 vendors by total invoice amount. For each, check if their invoices exceed PO values, check for duplicate invoices, and assess concentration risk. Which vendor poses the biggest threat to production continuity?",
    "Five-Why Investigation": "Query VW_DETECT_INVOICE_OVER_PO for the highest-impact overbilling event. Then investigate: traverse from the vendor to all their POs, check GR/IR match rates, look at which plants and materials are affected. Produce a Five-Why root cause analysis with ranked candidates.",
    "Plant Cascade Analysis": "For each plant (P100 Bengaluru, P200 Pune, P300 Chennai), identify the top supplier by PO value. Assess: if that supplier fails, what material groups are affected? Which product lines (Max Pro, Glide Lite, Urban Zip) would stop?",
    "AP Aging Summary": "Query open AP items. Group by vendor and age bucket (0-30, 31-60, 61-90, 90+ days). Identify vendors with the largest overdue amounts and assess payment risk.",
}


def call_agent(prompt, thread_id=None):
    """Call the Cortex Agent via DATA_AGENT_RUN and return parsed response."""
    conn = get_snowflake_connection()
    if conn is None:
        return None

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    # Include conversation history if thread exists
    if thread_id and "agent_history" in st.session_state:
        messages = st.session_state["agent_history"] + messages

    request_body = json.dumps({"messages": messages})
    # Escape single quotes in the request body for SQL
    escaped = request_body.replace("'", "''")

    sql = f"""SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(
        '{AGENT_FQN}',
        '{escaped}'
    )"""

    try:
        cur = conn.cursor()
        cur.execute(sql)
        result = cur.fetchone()
        cur.close()
        if result and result[0]:
            return json.loads(result[0])
    except Exception as e:
        st.error(f"Agent call failed: {e}")
    return None


def extract_text_from_response(response):
    """Extract readable text from agent response."""
    if not response or "content" not in response:
        return "No response received."

    parts = []
    for block in response.get("content", []):
        if block.get("type") == "text":
            parts.append(block["text"])
        elif block.get("type") == "thinking":
            # Optionally show thinking
            pass
    return "\n".join(parts)


def extract_thinking_from_response(response):
    """Extract thinking/reasoning from agent response."""
    if not response or "content" not in response:
        return ""
    for block in response.get("content", []):
        if block.get("type") == "thinking":
            return block.get("text", "")
    return ""


def extract_suggestions(response):
    """Extract suggested follow-up queries."""
    if not response or "content" not in response:
        return []
    for block in response.get("content", []):
        if block.get("type") == "suggested_queries":
            return [q["query"] for q in block.get("queries", block if isinstance(block, list) else [])]
        # Handle the nested format
        if "suggested_queries" in block:
            return [q["query"] for q in block["suggested_queries"]]
    return []


# --- UI ---
st.divider()

# Quick action buttons
st.subheader("Quick Investigations")
cols = st.columns(3)
selected_prompt = None
for i, (label, prompt) in enumerate(QUICK_PROMPTS.items()):
    with cols[i % 3]:
        if st.button(label, use_container_width=True):
            selected_prompt = prompt

st.divider()

# Custom prompt
custom = st.text_area("Or ask a custom question:", placeholder="e.g., Investigate vendor Abbott-Munoz for overbilling patterns across all plants...")

if st.button("Ask Agent", type="primary") and custom:
    selected_prompt = custom

# Execute
if selected_prompt:
    with st.status("Agent is reasoning...", expanded=True) as status:
        st.write(f"Prompt: _{selected_prompt[:100]}..._" if len(selected_prompt) > 100 else f"Prompt: _{selected_prompt}_")
        response = call_agent(selected_prompt)

        if response:
            status.update(label="Agent response received", state="complete")
        else:
            status.update(label="Agent failed to respond", state="error")

    if response:
        # Show thinking (collapsed)
        thinking = extract_thinking_from_response(response)
        if thinking:
            with st.expander("Agent Reasoning (internal thinking)", expanded=False):
                st.text(thinking)

        # Show main response
        text = extract_text_from_response(response)
        st.markdown(text)

        # Show suggested follow-ups
        suggestions = extract_suggestions(response)
        if suggestions:
            st.divider()
            st.caption("Suggested follow-up questions:")
            for s in suggestions:
                st.code(s, language=None)

        # Store in session for multi-turn
        if "agent_history" not in st.session_state:
            st.session_state["agent_history"] = []
        st.session_state["agent_history"].append(
            {"role": "user", "content": [{"type": "text", "text": selected_prompt}]}
        )
        st.session_state["agent_history"].append(
            {"role": "assistant", "content": response.get("content", [])}
        )

# Show conversation history
if "agent_history" in st.session_state and st.session_state["agent_history"]:
    st.divider()
    if st.button("Clear conversation"):
        st.session_state["agent_history"] = []
        st.rerun()

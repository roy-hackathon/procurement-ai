import streamlit as st
import json
import re
import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import is_connected, run_query_df, run_query
from components.persona import persona_selector

# Configure logging
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ask_data")

st.set_page_config(page_title="Ask Your Data", layout="wide", initial_sidebar_state="expanded")

st.title("Talk to Your Procurement Data")
st.caption("Ask natural language questions about invoices, POs, vendors, and spend — powered by Cortex Agent and Semantic View.")
persona = persona_selector()

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

AGENT_FQN = "SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT"

# Suggested questions
st.markdown("**Try asking:**")
suggestions = [
    "What is total spend by vendor in 2025?",
    "Which vendors have the highest invoice count?",
    "Show me spend trend by month for 2025",
    "What is the average invoice amount per plant?",
    "Which material groups have the most purchase orders?",
]
suggestion_cols = st.columns(len(suggestions))
for i, q in enumerate(suggestions):
    with suggestion_cols[i]:
        if st.button(q, key=f"sugg_{i}", use_container_width=True):
            st.session_state["ask_input"] = q

st.divider()

# Chat history
if "ask_history" not in st.session_state:
    st.session_state["ask_history"] = []

# Display history
for msg in st.session_state["ask_history"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg.get("data") is not None:
            st.dataframe(msg["data"], use_container_width=True, hide_index=True)

# Input
user_input = st.chat_input("Ask a question about your procurement data...")

# Handle suggestion button click
if "ask_input" in st.session_state and st.session_state["ask_input"]:
    user_input = st.session_state.pop("ask_input")

if user_input:
    logger.info(f"User question: {user_input}")
    st.session_state["ask_history"].append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    with st.chat_message("assistant"):
        with st.spinner("Querying semantic layer..."):
            try:
                sql_call = f"""
                    SELECT SNOWFLAKE.CORTEX.DATA_AGENT_RUN(
                        '{AGENT_FQN}',
                        $${{"messages": [{{"role": "user", "content": [{{"type": "text", "text": "{user_input.replace('"', '\\\\"')}"}}]}}]}}$$,
                        TRUE
                    ) AS RESPONSE
                """
                logger.debug(f"SQL call: {sql_call.strip()}")

                result = run_query(sql_call)
                logger.info(f"Agent returned {len(result)} row(s)")

                if result:
                    response_raw = result[0].get("RESPONSE", "{}")
                    logger.debug(f"Raw response (first 2000 chars): {str(response_raw)[:2000]}")

                    resp = json.loads(response_raw) if isinstance(response_raw, str) else response_raw
                    logger.info(f"Response keys: {list(resp.keys()) if isinstance(resp, dict) else type(resp)}")

                    # Extract text and SQL from response content blocks
                    text_parts = []
                    sql_part = None
                    content = resp.get("content", [])
                    logger.debug(f"Content blocks count: {len(content) if isinstance(content, list) else 'not a list'}")

                    if isinstance(content, list):
                        for idx, block in enumerate(content):
                            block_type = block.get("type", "unknown")
                            logger.debug(f"  Block {idx}: type={block_type}, keys={list(block.keys())}")

                            if block_type == "text":
                                txt = block.get("text", "")
                                logger.debug(f"  Text block {idx} (first 200): {txt[:200]}")
                                # Skip text blocks that are just SQL
                                if txt.strip().upper().startswith(("SELECT ", "WITH ", "CREATE ", "INSERT ")):
                                    sql_part = txt.strip()
                                    logger.info(f"  Found SQL in text block {idx}")
                                elif "```sql" in txt:
                                    sql_match = re.search(r"```sql\s*(.*?)```", txt, re.DOTALL)
                                    if sql_match:
                                        sql_part = sql_match.group(1).strip()
                                        logger.info(f"  Extracted SQL from markdown in block {idx}")
                                    non_sql = re.sub(r"```sql\s*.*?```", "", txt, flags=re.DOTALL).strip()
                                    if non_sql:
                                        text_parts.append(non_sql)
                                else:
                                    text_parts.append(txt)

                            elif block_type == "tool_use":
                                tool_use = block.get("tool_use", {})
                                logger.debug(f"  Tool use block {idx}: name={tool_use.get('name')}, input_keys={list(tool_use.get('input', {}).keys())}")
                                tool_input = tool_use.get("input", {})
                                if "sql" in tool_input:
                                    sql_part = tool_input["sql"]
                                    logger.info(f"  Found SQL in tool_use.input.sql")
                                elif "query" in tool_input:
                                    sql_part = tool_input["query"]
                                    logger.info(f"  Found SQL in tool_use.input.query")

                            elif block_type == "sql":
                                sql_part = block.get("statement", "")
                                logger.info(f"  Found SQL in sql-type block")

                            elif block_type == "thinking":
                                logger.debug(f"  Thinking block {idx} (skipped)")

                            elif block_type == "tool_result":
                                # Tool results may contain the actual data/text answer
                                tool_content = block.get("content", "")
                                if isinstance(tool_content, str) and tool_content.strip():
                                    logger.debug(f"  Tool result block {idx} (first 200): {tool_content[:200]}")
                                    # Check if it's JSON with sql or text
                                    try:
                                        tr = json.loads(tool_content)
                                        if isinstance(tr, dict) and "sql" in tr:
                                            sql_part = tr["sql"]
                                            logger.info(f"  Found SQL in tool_result JSON")
                                    except (json.JSONDecodeError, TypeError):
                                        # It's plain text from tool result
                                        if not tool_content.strip().upper().startswith(("SELECT ", "WITH ")):
                                            text_parts.append(tool_content)
                                elif isinstance(tool_content, list):
                                    for tc in tool_content:
                                        if isinstance(tc, dict) and tc.get("type") == "text":
                                            text_parts.append(tc.get("text", ""))

                            else:
                                logger.debug(f"  Unknown block type: {block_type}")

                    answer_text = "\n".join(text_parts) if text_parts else ""
                    logger.info(f"Extracted text length: {len(answer_text)}, SQL found: {sql_part is not None}")

                    # Strip any XML-like agent artifacts from text
                    answer_text = re.sub(r"<function_calls>.*?</function_calls>", "", answer_text, flags=re.DOTALL).strip()
                    answer_text = re.sub(r"</?function_calls>", "", answer_text).strip()
                    answer_text = re.sub(r"<.*?>.*?</.*?>", "", answer_text, flags=re.DOTALL).strip()
                    answer_text = re.sub(r"<[^>]*>", "", answer_text).strip() if "<" in answer_text and ("function" in answer_text or "antml" in answer_text) else answer_text

                    if sql_part:
                        logger.info(f"SQL to execute: {sql_part[:300]}")

                    # Execute SQL silently and show results as data
                    data_df = None
                    if sql_part:
                        try:
                            data_df = run_query_df(sql_part)
                            logger.info(f"SQL execution returned {len(data_df)} rows, {len(data_df.columns)} cols")
                        except Exception as e:
                            logger.error(f"SQL execution failed: {e}")

                    # Show natural language answer
                    if answer_text:
                        st.markdown(answer_text)
                        logger.debug(f"Displayed answer text: {answer_text[:200]}")

                    # Show data table if results exist
                    if data_df is not None and not data_df.empty:
                        st.dataframe(data_df, use_container_width=True, hide_index=True)
                    elif not answer_text:
                        st.markdown("Query executed successfully.")

                    # Show SQL in collapsed expander
                    if sql_part:
                        with st.expander("View SQL", expanded=False):
                            st.code(sql_part, language="sql")

                    final_text = answer_text if answer_text else "Here are the results:"
                    st.session_state["ask_history"].append({
                        "role": "assistant",
                        "content": final_text,
                        "data": data_df if data_df is not None and not data_df.empty else None,
                    })
                else:
                    logger.warning("Agent returned empty result")
                    st.warning("No response from agent.")
                    st.session_state["ask_history"].append({"role": "assistant", "content": "No response from agent."})

            except Exception as e:
                logger.error(f"Agent query failed: {e}", exc_info=True)
                error_msg = f"Agent query failed: {str(e)[:200]}"
                st.error(error_msg)
                st.session_state["ask_history"].append({"role": "assistant", "content": error_msg})

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

st.set_page_config(page_title="Ask Procurement AI", layout="wide", initial_sidebar_state="expanded")

st.title("Ask Procurement AI")
st.caption("Ask natural language questions about invoices, POs, vendors, and spend — powered by Cortex Agent and Semantic View.")
# persona removed

if not is_connected():
    st.error("Snowflake connection unavailable.")
    st.stop()

from components.sidebar_info import render_account_info
render_account_info()

AGENT_FQN = "SAP_P2P_FINANCE_DEV.ACTION.COCOEV_PROCUREMENT_AGENT"

# Rotating snowflake avatar for assistant responses
st.markdown("""
<style>
[data-testid="chatAvatarIcon-assistant"] {
    animation: spin 3s linear infinite;
}
@keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
}
</style>
""", unsafe_allow_html=True)

# Categorized suggested questions by procurement theme
QUESTION_THEMES = {
    "Spend Analysis": [
        "What is total spend by vendor in 2025?",
        "Show me spend trend by month for 2025",
        "Which material groups have the highest total spend?",
        "What is the spend distribution across plants?",
    ],
    "Vendor Performance": [
        "Which vendors have the highest invoice count?",
        "Show me top 5 vendors by total PO value",
        "Which vendors supply across all 3 plants?",
        "What is the average PO value per vendor?",
    ],
    "Invoice & Payments": [
        "What is the average invoice amount per plant?",
        "How many invoices are pending vs paid?",
        "Which vendors have the largest AP open balance?",
        "Show total invoiced amount by currency",
    ],
    "Inventory & Delivery": [
        "Which material groups have the most purchase orders?",
        "Show goods receipt volume by plant",
        "What is the GR vs IR value gap by vendor?",
        "Which plants receive the most deliveries?",
    ],
    "Risk & Compliance": [
        "Which vendors have invoices exceeding PO value?",
        "Show vendors with aging AP items over 60 days",
        "What is the total uninvoiced goods receipt exposure?",
        "Which vendors have the highest GR/IR mismatch?",
    ],
}

theme_tabs = st.tabs(list(QUESTION_THEMES.keys()))
for tab_idx, (theme, questions) in enumerate(QUESTION_THEMES.items()):
    with theme_tabs[tab_idx]:
        q_cols = st.columns(2)
        for i, q in enumerate(questions):
            with q_cols[i % 2]:
                if st.button(q, key=f"theme_{tab_idx}_{i}", use_container_width=True):
                    st.session_state["ask_input"] = q

st.divider()

# Chat history
if "ask_history" not in st.session_state:
    st.session_state["ask_history"] = []

# Display history
for msg in st.session_state["ask_history"]:
    avatar = "❄️" if msg["role"] == "assistant" else None
    with st.chat_message(msg["role"], avatar=avatar):
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

    with st.chat_message("assistant", avatar="❄️"):
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

                    # Check for internal error from agent
                    if isinstance(resp, dict) and resp.get("message") == "internal error":
                        raise RuntimeError(f"Cortex Agent returned internal error (code: {resp.get('code')}). Falling back to Cortex Complete.")

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
                logger.warning(f"Agent query failed: {e}. Trying fallback with CORTEX.COMPLETE...")
                # Fallback: use CORTEX.COMPLETE with semantic view context
                try:
                    safe_input = user_input.replace("'", "''").replace("\\", "\\\\")
                    prompt = (
                        "You are a procurement data analyst for CoCoEV, an electric scooter manufacturer. "
                        "Answer the following question using SQL against these tables in SAP_P2P_FINANCE_DEV.GOLD schema:\\n"
                        "- FCT_PURCHASE_ORDERS: PO_ID, PO_LINE, VENDOR_SK, MATERIAL_SK, PLANT_SK, NET_VALUE (NUMBER), GROSS_VALUE (NUMBER), QUANTITY (NUMBER), FISCAL_YEAR (NUMBER)\\n"
                        "- FCT_AP_INVOICES: INVOICE_ID, VENDOR_SK, GROSS_INVOICE_AMOUNT (NUMBER), FISCAL_YEAR (VARCHAR), DATE_KEY\\n"
                        "- FCT_AP_OPEN_ITEMS: COMPANY_CODE, DOC_ID, VENDOR_SK, AMOUNT_LOCAL_CURRENCY (NUMBER), DUE_DATE_KEY\\n"
                        "- FCT_GOODS_MOVEMENTS: MATERIAL_DOC, MATERIAL_SK, PLANT_SK, QUANTITY (NUMBER), AMOUNT_LOCAL_CURRENCY (NUMBER)\\n"
                        "- DIM_VENDOR: VENDOR_SK, VENDOR_NAME, VENDOR_ID, COUNTRY\\n"
                        "- DIM_MATERIAL: MATERIAL_SK, MATERIAL_DESCRIPTION, MATERIAL_TYPE, MATERIAL_GROUP\\n"
                        "- DIM_PLANT: PLANT_SK, PLANT_NAME, PLANT_ID\\n"
                        "- DIM_DATE: DATE_KEY, FULL_DATE, YEAR, MONTH, MONTH_NAME\\n\\n"
                        "Join facts to dimensions using SK columns (e.g. FCT_PURCHASE_ORDERS.VENDOR_SK = DIM_VENDOR.VENDOR_SK).\\n"
                        f"Question: {safe_input}\\n\\n"
                        "Return ONLY the SQL query. No markdown, no explanation, no backticks."
                    ).replace("'", "''")

                    fallback_sql = f"SELECT SNOWFLAKE.CORTEX.COMPLETE('llama3.1-70b', '{prompt}') AS RESPONSE"
                    logger.debug(f"Fallback SQL: {fallback_sql[:300]}")

                    fallback_result = run_query(fallback_sql)
                    logger.info(f"Fallback returned: {len(fallback_result)} row(s)")

                    if fallback_result:
                        generated_sql = fallback_result[0].get("RESPONSE", "").strip()
                        logger.info(f"Generated SQL (first 300): {generated_sql[:300]}")

                        # Clean up the response
                        generated_sql = generated_sql.strip('`').strip()
                        if generated_sql.lower().startswith("```"):
                            generated_sql = re.sub(r"```(?:sql)?\s*", "", generated_sql)
                            generated_sql = generated_sql.replace("```", "").strip()
                        if generated_sql.lower().startswith("sql\n"):
                            generated_sql = generated_sql[4:].strip()

                        # Add schema prefix if not present
                        for tbl in ["FCT_PURCHASE_ORDERS", "FCT_AP_INVOICES", "FCT_AP_OPEN_ITEMS", "FCT_PO_HISTORY",
                                    "FCT_GOODS_MOVEMENTS", "FCT_GL_TRANSACTIONS", "DIM_VENDOR", "DIM_MATERIAL",
                                    "DIM_PLANT", "DIM_DATE", "DIM_COMPANY_CODE", "DIM_VENDOR_COMPANY", "DIM_STORAGE_LOCATION"]:
                            generated_sql = re.sub(
                                rf'\b(?<!\.){tbl}\b',
                                f'SAP_P2P_FINANCE_DEV.GOLD.{tbl}',
                                generated_sql
                            )

                        if generated_sql.upper().startswith(("SELECT", "WITH")):
                            try:
                                data_df = run_query_df(generated_sql)
                                if data_df is not None and not data_df.empty:
                                    st.markdown(f"Here are the results for: *{user_input}*")
                                    st.dataframe(data_df, use_container_width=True, hide_index=True)
                                    with st.expander("View SQL", expanded=False):
                                        st.code(generated_sql, language="sql")
                                    st.session_state["ask_history"].append({
                                        "role": "assistant",
                                        "content": f"Results for: {user_input}",
                                        "data": data_df,
                                    })
                                else:
                                    st.info("Query returned no results.")
                                    with st.expander("View SQL", expanded=False):
                                        st.code(generated_sql, language="sql")
                                    st.session_state["ask_history"].append({"role": "assistant", "content": "Query returned no results."})
                            except Exception as sql_err:
                                logger.error(f"Generated SQL execution failed: {sql_err}")
                                st.warning(f"Generated SQL failed to execute: {str(sql_err)[:150]}")
                                with st.expander("View Generated SQL", expanded=True):
                                    st.code(generated_sql, language="sql")
                                st.session_state["ask_history"].append({"role": "assistant", "content": f"SQL generation succeeded but execution failed: {str(sql_err)[:100]}"})
                        else:
                            # Model returned natural language instead of SQL
                            st.markdown(generated_sql)
                            st.session_state["ask_history"].append({"role": "assistant", "content": generated_sql})
                    else:
                        st.error("No response from fallback model.")
                        st.session_state["ask_history"].append({"role": "assistant", "content": "No response from fallback."})
                except Exception as fallback_err:
                    logger.error(f"Fallback also failed: {fallback_err}", exc_info=True)
                    error_msg = f"Query failed: {str(fallback_err)[:200]}"
                    st.error(error_msg)
                    st.session_state["ask_history"].append({"role": "assistant", "content": error_msg})

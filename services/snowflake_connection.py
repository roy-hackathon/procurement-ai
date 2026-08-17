import streamlit as st

def get_snowflake_connection():
    """Get Snowflake connection - supports Streamlit-in-Snowflake (SiS) and external."""
    if "snowflake_conn" in st.session_state and st.session_state["snowflake_conn"] is not None:
        return st.session_state["snowflake_conn"]

    # Mode 1: Streamlit-in-Snowflake (SiS)
    try:
        from snowflake.snowpark.context import get_active_session
        session = get_active_session()
        st.session_state["snowflake_conn"] = session
        st.session_state["conn_mode"] = "sis"
        return session
    except Exception:
        pass

    # Mode 2: External via snowflake-connector-python
    try:
        import snowflake.connector
        conn = snowflake.connector.connect(
            account=st.secrets.get("SNOWFLAKE_ACCOUNT", ""),
            user=st.secrets.get("SNOWFLAKE_USER", ""),
            password=st.secrets.get("SNOWFLAKE_PASSWORD", ""),
            warehouse=st.secrets.get("SNOWFLAKE_WAREHOUSE", "COMPUTE_WH"),
            database=st.secrets.get("SNOWFLAKE_DATABASE", "SAP_P2P_FINANCE_DEV"),
            schema=st.secrets.get("SNOWFLAKE_SCHEMA", "GOLD"),
            role=st.secrets.get("SNOWFLAKE_ROLE", "ACCOUNTADMIN"),
        )
        st.session_state["snowflake_conn"] = conn
        st.session_state["conn_mode"] = "connector"
        return conn
    except Exception as e:
        st.session_state["snowflake_conn"] = None
        st.session_state["conn_error"] = str(e)
        return None


def run_query(sql, params=None):
    """Execute a query and return results as list of dicts."""
    conn = get_snowflake_connection()
    if conn is None:
        return []
    try:
        mode = st.session_state.get("conn_mode", "connector")
        if mode == "sis":
            df = conn.sql(sql).to_pandas()
            return df.to_dict("records")
        else:
            cur = conn.cursor()
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        st.error(f"Query failed: {e}")
        return []


def run_query_df(sql, params=None):
    """Execute a query and return a pandas DataFrame."""
    import pandas as pd
    conn = get_snowflake_connection()
    if conn is None:
        return pd.DataFrame()
    try:
        mode = st.session_state.get("conn_mode", "connector")
        if mode == "sis":
            return conn.sql(sql).to_pandas()
        else:
            cur = conn.cursor()
            if params:
                cur.execute(sql, params)
            else:
                cur.execute(sql)
            columns = [desc[0] for desc in cur.description]
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=columns)
    except Exception as e:
        st.error(f"Query failed: {e}")
        return pd.DataFrame()


def is_connected():
    """Check if Snowflake connection is active."""
    conn = get_snowflake_connection()
    return conn is not None

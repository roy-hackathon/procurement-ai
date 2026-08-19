import streamlit as st


def inject_css():
    """Modern CSS — shadows, rounded cards, modern buttons."""
    st.markdown("""<style>
/* KPI metric cards */
[data-testid="stMetric"] {
    background: white;
    border: none;
    border-radius: 12px;
    padding: 16px 20px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06), 0 0 1px rgba(0, 0, 0, 0.1);
}

/* Buttons — modern rounded */
.stButton > button {
    border-radius: 8px;
    font-weight: 500;
    padding: 0.5rem 1.25rem;
    border: 1px solid #e2e8f0;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
    transition: all 0.15s ease;
}
.stButton > button:hover {
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.12);
    transform: translateY(-1px);
}

/* Dataframes */
[data-testid="stDataFrame"] {
    border-radius: 8px;
    overflow: hidden;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}

/* Dividers — lighter */
hr {
    border: none;
    border-top: 1px solid #f1f5f9;
    margin: 1.5rem 0;
}

/* Status component */
[data-testid="stStatus"] {
    border-radius: 12px;
    border: none;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}
</style>""", unsafe_allow_html=True)

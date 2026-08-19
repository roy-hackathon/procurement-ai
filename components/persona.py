import streamlit as st
from config.personas import PERSONAS, DEFAULT_PERSONA


def persona_selector():
    """Render persona selector in the sidebar. Returns selected persona code."""
    if "persona" not in st.session_state:
        st.session_state["persona"] = DEFAULT_PERSONA

    codes = list(PERSONAS.keys())
    labels = list(PERSONAS.values())
    current_idx = codes.index(st.session_state["persona"]) if st.session_state["persona"] in codes else 0

    with st.sidebar:
        st.markdown("#### 👤 Persona")
        selected_label = st.selectbox(
            "View as",
            labels,
            index=current_idx,
            key="persona_select",
            label_visibility="collapsed",
        )
        selected_code = codes[labels.index(selected_label)]
        st.session_state["persona"] = selected_code
        if selected_code != "general":
            st.markdown(
                f'<div style="background:linear-gradient(135deg,#FF6B35,#E63946);color:white;'
                f'padding:8px 12px;border-radius:8px;font-size:13px;font-weight:600;text-align:center;">'
                f'Viewing as {selected_label}</div>',
                unsafe_allow_html=True,
            )
        st.divider()

    return selected_code


def get_persona():
    """Get current persona code from session state."""
    return st.session_state.get("persona", DEFAULT_PERSONA)


def is_general():
    """Check if current persona is General (sees everything)."""
    return get_persona() == "general"


def get_persona_display_name():
    """Get display name for current persona."""
    code = get_persona()
    return PERSONAS.get(code, code)

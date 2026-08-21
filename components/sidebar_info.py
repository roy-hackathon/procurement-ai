"""Sidebar account info component — shows masked account, region, edition, credit health."""
import streamlit as st
from services.snowflake_connection import run_query


def render_account_info():
    """Render Snowflake account info box in the sidebar."""
    with st.sidebar:
        # Submission info (always visible, above account details)
        st.markdown("""
        <div style="background:#faf5ff;border:1px solid #e9d5ff;border-radius:8px;padding:14px 16px;margin-bottom:12px;">
            <table style="width:100%;font-size:12px;color:#334155;">
                <tr><td style="padding:2px 0;color:#6b7280;">Submitted By</td><td style="padding:2px 0;font-weight:700;color:#7c3aed;">Hiresh Roy</td></tr>
                <tr><td style="padding:2px 0;color:#6b7280;">Track</td><td style="padding:2px 0;font-weight:600;color:#7c3aed;">Intelligent Workflow Automation Agent</td></tr>
            </table>
        </div>
        """, unsafe_allow_html=True)

        try:
            acct_info = run_query("SELECT CURRENT_ACCOUNT_NAME() AS ACCT, CURRENT_REGION() AS REGION, CURRENT_ORGANIZATION_NAME() AS ORG")
            acct_name = acct_info[0]["ACCT"] if acct_info else "Unknown"
            region = acct_info[0]["REGION"] if acct_info else "Unknown"
            org_name = acct_info[0]["ORG"] if acct_info else ""

            # Mask: org shows first 2 + rest as x, account shows last 3 + rest as x
            def _mask_start(val):
                """Show first 2 chars, mask the rest."""
                if not val or len(val) <= 2:
                    return val
                return val[:2] + "x" * (len(val) - 2)

            def _mask_end(val):
                """Show last 3 chars, mask the rest."""
                if not val or len(val) <= 3:
                    return val
                return "x" * (len(val) - 3) + val[-3:]

            masked_org = _mask_start(org_name)
            masked_acct = _mask_end(acct_name)

            st.markdown(f"""
            <div style="background:#f0f9ff;border:1px solid #bae6fd;border-radius:8px;padding:14px 16px;margin-bottom:16px;">
                <div style="font-weight:700;font-size:13px;color:#0369a1;margin-bottom:8px;">❄️ Snowflake Account</div>
                <table style="width:100%;font-size:12px;color:#334155;">
                    <tr><td style="padding:2px 0;color:#6b7280;">Account</td><td style="padding:2px 0;font-weight:600;">{masked_org}-{masked_acct}</td></tr>
                    <tr><td style="padding:2px 0;color:#6b7280;">Region</td><td style="padding:2px 0;">{region}</td></tr>
                    <tr><td style="padding:2px 0;color:#6b7280;">Edition</td><td style="padding:2px 0;">Enterprise (Free Trial)</td></tr>
                    <tr><td style="padding:2px 0;color:#6b7280;">Cortex Agent</td><td style="padding:2px 0;font-weight:600;color:#059669;">Enabled</td></tr>
                    <tr><td style="padding:2px 0;color:#6b7280;">AI Function</td><td style="padding:2px 0;font-weight:600;color:#059669;">Enabled</td></tr>
                </table>
            </div>
            """, unsafe_allow_html=True)
        except Exception:
            pass

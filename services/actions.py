from services.snowflake_connection import run_query
import streamlit as st


def place_payment_hold(invoice_id, reason, persona_code="procurement_controller"):
    result = run_query(f"""
        INSERT INTO SAP_P2P_FINANCE_DEV.ACTION.PAYMENT_HOLD
            (INVOICE_ID, REASON_CODE, REASON_TEXT, IS_ACTIVE)
        VALUES ('{invoice_id}', 'AGENT_HOLD', '{reason}', TRUE)
    """)
    return True


def send_notification(run_id, persona_code, subject, body_html):
    result = run_query(f"""
        CALL SAP_P2P_FINANCE_DEV.ACTION.SP_SEND_NOTIFICATION(
            '{run_id}', NULL, '{persona_code}', '{subject}', '{body_html}', FALSE
        )
    """)
    return result


def approve_action(approval_id, decided_by, note="Approved via dashboard"):
    run_query(f"""
        UPDATE SAP_P2P_FINANCE_DEV.ACTION.APPROVAL_QUEUE
        SET DECISION = 'approved', DECIDED_AT = CURRENT_TIMESTAMP(),
            DECIDED_BY = '{decided_by}', DECISION_NOTE = '{note}'
        WHERE APPROVAL_ID = {approval_id}
    """)
    return True


def reject_action(approval_id, decided_by, note="Rejected via dashboard"):
    run_query(f"""
        UPDATE SAP_P2P_FINANCE_DEV.ACTION.APPROVAL_QUEUE
        SET DECISION = 'rejected', DECIDED_AT = CURRENT_TIMESTAMP(),
            DECIDED_BY = '{decided_by}', DECISION_NOTE = '{note}'
        WHERE APPROVAL_ID = {approval_id}
    """)
    return True

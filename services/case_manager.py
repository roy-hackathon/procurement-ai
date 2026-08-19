"""
case_manager.py — CRUD operations for AI_PROCUREMENT_CASE and AI_AUDIT_LOG.
Provides the transactional layer for the Control Tower.
"""

import uuid
from datetime import datetime
from services.snowflake_connection import run_query, run_query_df

DATABASE = "SAP_P2P_FINANCE_DEV"


def _uid():
    return uuid.uuid4().hex[:16].upper()


def create_case(case_type, entity_id, vendor_id, vendor_name, headline,
                financial_impact, severity, event_id, run_id, owner="procurement_manager"):
    case_id = f"CASE-{datetime.utcnow().strftime('%Y%m%d')}-{_uid()[:6]}"
    run_query(f"""
        INSERT INTO {DATABASE}.ACTION.AI_PROCUREMENT_CASE
        (CASE_ID, CASE_TYPE, ENTITY_TYPE, ENTITY_ID, VENDOR_ID, VENDOR_NAME,
         RISK_LEVEL, FINANCIAL_IMPACT, STATUS, OWNER, RUN_ID, EVENT_ID, HEADLINE)
        VALUES ('{case_id}', '{case_type}', 'VENDOR', '{entity_id}', '{vendor_id}',
                '{vendor_name.replace("'", "''")}', '{severity}', {financial_impact},
                'NEW', '{owner}', '{run_id}', {event_id},
                '{headline.replace("'", "''")[:300]}')
    """)
    audit_log(case_id, "CASE_CREATED", "SYSTEM", "Detection Agent",
              f"Case created for {vendor_name}: {headline[:100]}")
    return case_id


def update_case_status(case_id, new_status, **kwargs):
    sets = [f"STATUS = '{new_status}'", "UPDATED_AT = CURRENT_TIMESTAMP()"]
    if "risk_level" in kwargs:
        sets.append(f"RISK_LEVEL = '{kwargs['risk_level']}'")
    if "risk_score" in kwargs:
        sets.append(f"RISK_SCORE = {kwargs['risk_score']}")
    if "investigation_id" in kwargs:
        sets.append(f"INVESTIGATION_ID = {kwargs['investigation_id']}")
    if "risk_id" in kwargs:
        sets.append(f"RISK_ID = {kwargs['risk_id']}")
    if "root_cause" in kwargs:
        sets.append(f"ROOT_CAUSE = '{str(kwargs['root_cause']).replace(chr(39), chr(39)+chr(39))[:200]}'")
    if "recommendation" in kwargs:
        sets.append(f"RECOMMENDATION = '{str(kwargs['recommendation']).replace(chr(39), chr(39)+chr(39))[:500]}'")

    run_query(f"UPDATE {DATABASE}.ACTION.AI_PROCUREMENT_CASE SET {', '.join(sets)} WHERE CASE_ID = '{case_id}'")


def get_cases(status=None, limit=50):
    where = f"WHERE STATUS = '{status}'" if status else ""
    return run_query_df(f"""
        SELECT * FROM {DATABASE}.ACTION.AI_PROCUREMENT_CASE
        {where}
        ORDER BY
            CASE RISK_LEVEL WHEN 'CRITICAL' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,
            FINANCIAL_IMPACT DESC
        LIMIT {limit}
    """)


def get_case(case_id):
    rows = run_query(f"SELECT * FROM {DATABASE}.ACTION.AI_PROCUREMENT_CASE WHERE CASE_ID = '{case_id}'")
    return rows[0] if rows else None


def get_case_by_event(event_id):
    rows = run_query(f"SELECT * FROM {DATABASE}.ACTION.AI_PROCUREMENT_CASE WHERE EVENT_ID = {event_id} LIMIT 1")
    return rows[0] if rows else None


def audit_log(case_id, event_type, actor_type, actor, description, metadata=None):
    aid = f"AUD-{_uid()}"
    meta_sql = f"PARSE_JSON('{metadata}')" if metadata else "NULL"
    run_query(f"""
        INSERT INTO {DATABASE}.ACTION.AI_AUDIT_LOG
        (AUDIT_ID, CASE_ID, EVENT_TYPE, ACTOR_TYPE, ACTOR, DESCRIPTION, METADATA)
        VALUES ('{aid}', '{case_id}', '{event_type}', '{actor_type}', '{actor}',
                '{description.replace("'", "''")[:2000]}', {meta_sql})
    """)


def get_audit_trail(case_id):
    return run_query_df(f"""
        SELECT EVENT_TYPE, ACTOR_TYPE, ACTOR, DESCRIPTION, CREATED_AT
        FROM {DATABASE}.ACTION.AI_AUDIT_LOG
        WHERE CASE_ID = '{case_id}'
        ORDER BY CREATED_AT
    """)


def execute_action(case_id, action_type, actor_persona):
    """Execute an action on a case (payment hold, etc.) and update state."""
    update_case_status(case_id, "ACTION_EXECUTED")
    audit_log(case_id, "ACTION_EXECUTED", "USER", actor_persona,
              f"{action_type} executed by {actor_persona}")
    return True


def get_kpi_summary():
    """Get operational KPIs for the Control Tower header."""
    result = run_query(f"""
        SELECT
            COUNT(*) AS TOTAL_CASES,
            SUM(CASE WHEN RISK_LEVEL IN ('CRITICAL','HIGH') THEN 1 ELSE 0 END) AS HIGH_RISK_CASES,
            SUM(CASE WHEN STATUS IN ('NEW','INVESTIGATING','AI_INVESTIGATED','AWAITING_DECISION') THEN 1 ELSE 0 END) AS OPEN_CASES,
            SUM(CASE WHEN STATUS = 'AWAITING_DECISION' THEN 1 ELSE 0 END) AS ACTIONS_PENDING,
            SUM(CASE WHEN STATUS = 'ACTION_EXECUTED' THEN 1 ELSE 0 END) AS ACTIONS_EXECUTED,
            COALESCE(SUM(FINANCIAL_IMPACT), 0) AS TOTAL_EXPOSURE,
            COALESCE(SUM(CASE WHEN STATUS = 'ACTION_EXECUTED' THEN FINANCIAL_IMPACT ELSE 0 END), 0) AS SAVINGS
        FROM {DATABASE}.ACTION.AI_PROCUREMENT_CASE
    """)
    return result[0] if result else {}

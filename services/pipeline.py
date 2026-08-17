"""
pipeline.py — Full 5-phase pipeline logic ported from the ai-business-event-detector scripts.
Runs entirely via snowflake-connector-python (no CLI, no CoCo, no local files needed).
"""

import json
import hashlib
from datetime import datetime


DATABASE = "SAP_P2P_FINANCE_DEV"
SYSTEMIC_VENDOR_THRESHOLD = 5

# Phase 4 playbooks (from run_action_planning.py)
MITIGATION_PLAYBOOK = {
    "duplicate_ir": [("payment_hold", "auto", "internal"), ("notify_persona", "auto", "email"), ("create_incident_summary", "auto", "document_store")],
    "no_goods_receipt": [("notify_persona", "auto", "email"), ("create_incident_summary", "auto", "document_store")],
    "over_delivery": [("notify_persona", "auto", "email")],
    "price_variance": [("notify_persona", "auto", "email"), ("draft_sap_change_request", "draft_and_approve", "sap_draft"), ("create_incident_summary", "auto", "document_store")],
    "currency_control_gap": [("notify_persona", "auto", "email")],
    "goods_receipt_no_invoice": [("notify_persona", "auto", "email")],
    "payment_terms_drift": [("notify_persona", "auto", "email"), ("create_incident_summary", "auto", "document_store")],
    "indeterminate": [("notify_persona", "notify_only", "email")],
}

PREVENTION_PLAYBOOK = {
    "duplicate_ir": [("recommend_control_improvement", "notify_only", "email")],
    "price_variance": [("recommend_vendor_review", "notify_only", "email")],
    "goods_receipt_no_invoice": [("recommend_control_improvement", "notify_only", "email")],
    "payment_terms_drift": [("recommend_control_improvement", "notify_only", "email")],
}

BRANCH_RISK_CATEGORY = {
    "duplicate_ir": ("financial", ["operational"]),
    "no_goods_receipt": ("financial", ["compliance"]),
    "over_delivery": ("operational", ["financial"]),
    "price_variance": ("financial", ["strategic"]),
    "currency_control_gap": ("data_quality", ["financial"]),
    "goods_receipt_no_invoice": ("financial", ["operational"]),
    "payment_terms_drift": ("compliance", ["financial"]),
    "indeterminate": ("data_quality", []),
}

CASCADE_TEMPLATES = {
    "duplicate_ir": ["duplicate_payment", "cash_outflow", "ap_reconciliation_burden"],
    "no_goods_receipt": ["unverified_liability", "audit_exception", "cash_outflow"],
    "over_delivery": ["excess_inventory", "working_capital_tie_up"],
    "price_variance": ["margin_erosion", "budget_variance", "vendor_renegotiation_needed"],
    "currency_control_gap": ["unquantifiable_exposure", "period_close_delay"],
    "goods_receipt_no_invoice": ["ap_accrual_risk", "period_close_delay", "vendor_relationship_risk"],
    "payment_terms_drift": ["unauthorized_terms_exposure", "audit_finding"],
    "indeterminate": ["unknown_exposure"],
}

OWNER_MAP = {
    "duplicate_ir": "ap_manager",
    "no_goods_receipt": "ap_clerk",
    "over_delivery": "buyer",
    "price_variance": "category_manager",
    "currency_control_gap": "controller",
    "goods_receipt_no_invoice": "ap_clerk",
    "payment_terms_drift": "controller",
    "indeterminate": "controller",
}


def _query(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        cur.close()


def _execute(conn, sql, params=None):
    cur = conn.cursor()
    try:
        cur.execute(sql, params or ())
        return cur.rowcount or 0
    finally:
        cur.close()


def _v(obj):
    return json.dumps(obj, default=str)


def _idempotency_key(*parts):
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def next_run_id():
    return f"RUN_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


# ============================================================
# PHASE 1: DETECTION
# ============================================================

def phase_detect(conn, run_id, progress_callback=None):
    detectors = _query(conn, f"SELECT * FROM {DATABASE}.ACTION.DETECTOR_REGISTRY WHERE IS_ACTIVE")
    total_inserted = 0
    total_refreshed = 0
    errors = []

    for det in detectors:
        det_name = det["DETECTOR_NAME"]
        view_name = f"{DATABASE}.ACTION.{det['VIEW_NAME']}"
        if progress_callback:
            progress_callback(f"Running detector: {det_name}")
        try:
            events = _query(conn, f"SELECT * FROM {view_name}")
            for evt in events:
                entity_key = evt.get("ENTITY_KEY", "")
                event_type = evt.get("EVENT_TYPE", det["EVENT_TYPE"])
                existing = _query(conn, f"SELECT EVENT_ID FROM {DATABASE}.ACTION.BUSINESS_EVENT WHERE EVENT_TYPE = %s AND ENTITY_KEY = %s", (event_type, entity_key))
                if existing:
                    _execute(conn, f"UPDATE {DATABASE}.ACTION.BUSINESS_EVENT SET LAST_SEEN_AT = CURRENT_TIMESTAMP(), SEEN_COUNT = SEEN_COUNT + 1 WHERE EVENT_TYPE = %s AND ENTITY_KEY = %s", (event_type, entity_key))
                    total_refreshed += 1
                else:
                    impact = float(evt.get("IMPACT_USD", 0) or 0)
                    _execute(conn, f"""
                        INSERT INTO {DATABASE}.ACTION.BUSINESS_EVENT
                        (RUN_ID, EVENT_TYPE, PATTERN_CLASS, DOMAIN_PACK, ENTITY_KEY, SEVERITY, IMPACT_USD, HEADLINE, DESCRIPTION, AFFECTED_KEYS, STATUS, DETECTED_AT, LAST_SEEN_AT, SEEN_COUNT, PERIOD_KEY)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), 1, %s)
                    """, (run_id, event_type, evt.get("PATTERN_CLASS", "threshold_breach"), det.get("DOMAIN_PACK", "finance"),
                          entity_key, evt.get("SEVERITY", "MEDIUM"), impact, evt.get("HEADLINE", f"{det_name}: {entity_key}"),
                          evt.get("DESCRIPTION", ""), _v(evt.get("AFFECTED_KEYS", {})),
                          datetime.utcnow().strftime("%Y-%m")))
                    total_inserted += 1
        except Exception as e:
            errors.append(f"{det_name}: {e}")

    return {"inserted": total_inserted, "refreshed": total_refreshed, "detectors": len(detectors), "errors": errors}


# ============================================================
# PHASE 2: INVESTIGATION
# ============================================================

def phase_investigate(conn, run_id, limit=50, progress_callback=None):
    events = _query(conn, f"""
        SELECT e.* FROM {DATABASE}.ACTION.BUSINESS_EVENT e
        LEFT JOIN {DATABASE}.ACTION.INVESTIGATION i ON i.EVENT_ID = e.EVENT_ID
        WHERE e.STATUS = 'open' AND i.INVESTIGATION_ID IS NULL
        ORDER BY ABS(e.IMPACT_USD) DESC LIMIT %s
    """, (limit,))

    results = []
    for event in events:
        if progress_callback:
            progress_callback(f"Investigating: {event.get('HEADLINE', event['EVENT_TYPE'])[:60]}")
        try:
            result = _investigate_event(conn, run_id, event)
            results.append(result)
        except Exception as e:
            results.append({"event_id": event["EVENT_ID"], "branch": "error", "confidence": 0, "error": str(e)})
    return results


def _investigate_event(conn, run_id, event):
    etype = event["EVENT_TYPE"]
    branch = "indeterminate"
    confidence = 0.30
    hypotheses = [{"branch": "indeterminate", "score": 0.30, "reason": f"Default for {etype}"}]
    narrative = f"Event {event['EVENT_ID']} ({etype}) on entity {event['ENTITY_KEY']}."
    evidence_complete = False
    missing = None

    if etype == "invoice_over_po":
        branch, confidence, hypotheses, evidence_complete, missing = "price_variance", 0.85, [
            {"branch": "price_variance", "score": 0.85, "reason": "Invoice exceeds PO value — price uplift likely"},
            {"branch": "duplicate_ir", "score": 0.40, "reason": "Possible duplicate invoice receipt"},
        ], True, None
        narrative = f"Invoice overbilling detected on {event['ENTITY_KEY']}. Primary hypothesis: price variance by vendor."
    elif etype == "duplicate_invoice_receipt":
        branch, confidence, hypotheses = "duplicate_ir", 0.90, [{"branch": "duplicate_ir", "score": 0.90, "reason": "Multiple invoice receipts against one PO line"}]
        evidence_complete = True
        narrative = f"Duplicate invoice receipt detected on {event['ENTITY_KEY']}. Payment hold recommended."
    elif etype == "po_invoice_currency_mismatch":
        branch, confidence, hypotheses = "currency_control_gap", 0.95, [{"branch": "currency_control_gap", "score": 0.95, "reason": "PO and invoice currencies differ"}]
        evidence_complete = True
        narrative = f"Currency mismatch on {event['ENTITY_KEY']}. Three-way match cannot be evaluated."
    elif etype == "grir_aging":
        branch, confidence, hypotheses = "goods_receipt_no_invoice", 0.85, [{"branch": "goods_receipt_no_invoice", "score": 0.85, "reason": "Goods received with no matching invoice"}]
        evidence_complete = True
        narrative = f"GR/IR aging on {event['ENTITY_KEY']}. Open exposure without invoice."
    elif etype == "unusual_payment_terms":
        branch, confidence, hypotheses = "payment_terms_drift", 0.70, [{"branch": "payment_terms_drift", "score": 0.70, "reason": "Payment terms deviate from vendor baseline"}]
        evidence_complete = False
        missing = "no corroborating signal beyond terms comparison"
        narrative = f"Payment terms anomaly on {event['ENTITY_KEY']}. Master-data governance review needed."
    elif etype in ("ap_open_item_aging",):
        branch, confidence, hypotheses = "goods_receipt_no_invoice", 0.75, [{"branch": "goods_receipt_no_invoice", "score": 0.75, "reason": "AP open item aging beyond normal cycle"}]
        evidence_complete = True
        narrative = f"AP aging detected on {event['ENTITY_KEY']}."

    # Persist to INVESTIGATION
    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.INVESTIGATION
        (EVENT_ID, RUN_ID, ROOT_CAUSE_BRANCH, CONFIDENCE, IMPACT_USD, EVIDENCE, HYPOTHESES, EVIDENCE_COMPLETE, MISSING_EVIDENCE, NARRATIVE)
        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, %s, %s
    """, (event["EVENT_ID"], run_id, branch, confidence, float(event.get("IMPACT_USD") or 0),
          _v({}), _v(hypotheses), evidence_complete, missing, narrative))
    _execute(conn, f"UPDATE {DATABASE}.ACTION.BUSINESS_EVENT SET STATUS = 'investigating' WHERE EVENT_ID = %s", (event["EVENT_ID"],))

    return {"event_id": event["EVENT_ID"], "branch": branch, "confidence": confidence, "impact_usd": float(event.get("IMPACT_USD") or 0)}


# ============================================================
# PHASE 3: RISK ASSESSMENT
# ============================================================

def phase_risk(conn, run_id, limit=50, progress_callback=None):
    investigations = _query(conn, f"""
        SELECT i.*, e.SEVERITY, e.HEADLINE, e.ENTITY_KEY
        FROM {DATABASE}.ACTION.INVESTIGATION i
        JOIN {DATABASE}.ACTION.BUSINESS_EVENT e ON e.EVENT_ID = i.EVENT_ID
        LEFT JOIN {DATABASE}.ACTION.RISK_ASSESSMENT r ON r.INVESTIGATION_ID = i.INVESTIGATION_ID
        WHERE r.RISK_ID IS NULL
        ORDER BY ABS(i.IMPACT_USD) DESC LIMIT %s
    """, (limit,))

    results = []
    for inv in investigations:
        if progress_callback:
            progress_callback(f"Assessing risk: {inv.get('HEADLINE', '')[:60]}")
        result = _assess_risk(conn, run_id, inv)
        results.append(result)
    return results


def _assess_risk(conn, run_id, inv):
    severity_score = {"CRITICAL": 100, "HIGH": 75, "MEDIUM": 50, "LOW": 25}.get(inv.get("SEVERITY", "MEDIUM"), 40)
    impact = float(inv.get("IMPACT_USD") or 0)
    impact_score = min(100, (impact / 20000) * 100) if impact > 0 else 10
    confidence = float(inv.get("CONFIDENCE") or 0.5)
    evidence_complete = bool(inv.get("EVIDENCE_COMPLETE"))
    operational_score = 60 if not evidence_complete else 40
    branch = inv.get("ROOT_CAUSE_BRANCH") or "indeterminate"
    dependency_score = 70 if branch in ("duplicate_ir", "no_goods_receipt") else 40

    composite = (0.25 * severity_score + 0.20 * impact_score + 0.20 * operational_score +
                 0.15 * dependency_score + 0.10 * 50 + 0.10 * (confidence * 100))

    if composite >= 80: priority = "P1"
    elif composite >= 60: priority = "P2"
    elif composite >= 35: priority = "P3"
    else: priority = "P4"

    primary, secondary = BRANCH_RISK_CATEGORY.get(branch, ("operational", []))
    cascade = CASCADE_TEMPLATES.get(branch, ["unknown_exposure"])
    owner = OWNER_MAP.get(branch, "controller")
    likelihood = "high" if inv.get("SEVERITY") in ("CRITICAL", "HIGH") else "medium"
    impact_level = {"CRITICAL": "critical", "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}.get(inv.get("SEVERITY", "MEDIUM"), "medium")

    narrative = (f"{inv.get('HEADLINE', '')} Root cause: {branch} (confidence {confidence:.2f}). "
                 f"Risk score {composite:.0f}/100 -> priority {priority}. Primary risk: {primary}. "
                 f"Cascade: {' -> '.join(cascade)}.")

    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.RISK_ASSESSMENT
        (EVENT_ID, INVESTIGATION_ID, RUN_ID, RISK_SCORE, PRIORITY, PRIMARY_RISK_CATEGORY,
         SECONDARY_RISK_CATEGORIES, LIKELIHOOD, IMPACT_LEVEL, FINANCIAL_IMPACT_USD,
         CASCADE_PATH, RECOMMENDED_OWNER, NARRATIVE)
        SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s, PARSE_JSON(%s), %s, %s
    """, (inv["EVENT_ID"], inv["INVESTIGATION_ID"], run_id, round(composite, 2), priority, primary,
          _v(secondary), likelihood, impact_level, impact, _v(cascade), owner, narrative))
    _execute(conn, f"UPDATE {DATABASE}.ACTION.BUSINESS_EVENT SET STATUS = 'risk_assessed' WHERE EVENT_ID = %s", (inv["EVENT_ID"],))

    return {"event_id": inv["EVENT_ID"], "priority": priority, "score": round(composite, 2), "owner": owner, "impact_usd": impact}


# ============================================================
# PHASE 4: ACTION PLANNING
# ============================================================

def phase_plan(conn, run_id, limit=50, progress_callback=None):
    risks = _query(conn, f"""
        SELECT r.*, i.ROOT_CAUSE_BRANCH, i.NARRATIVE AS INVESTIGATION_NARRATIVE
        FROM {DATABASE}.ACTION.RISK_ASSESSMENT r
        JOIN {DATABASE}.ACTION.INVESTIGATION i ON i.INVESTIGATION_ID = r.INVESTIGATION_ID
        LEFT JOIN {DATABASE}.ACTION.ACTION_PLAN p ON p.RISK_ID = r.RISK_ID
        WHERE p.PLAN_ID IS NULL
        ORDER BY r.RISK_SCORE DESC LIMIT %s
    """, (limit,))

    results = []
    for risk in risks:
        if progress_callback:
            progress_callback(f"Planning actions for {risk.get('RECOMMENDED_OWNER', 'unknown')} (P{risk.get('PRIORITY', '?')})")
        result = _plan_for_risk(conn, run_id, risk)
        results.append(result)
    return results


def _plan_for_risk(conn, run_id, risk):
    branch = risk.get("ROOT_CAUSE_BRANCH") or "indeterminate"
    priority = risk.get("PRIORITY", "P3")
    window = {"P1": "immediate", "P2": "today", "P3": "this_week", "P4": "monitor"}.get(priority, "this_week")

    mitigation_steps = MITIGATION_PLAYBOOK.get(branch, MITIGATION_PLAYBOOK["indeterminate"])
    _create_plan(conn, run_id, risk, "mitigation", mitigation_steps, window)

    if priority in ("P1", "P2") and branch in PREVENTION_PLAYBOOK:
        _create_plan(conn, run_id, risk, "prevention", PREVENTION_PLAYBOOK[branch], "this_month")

    _execute(conn, f"UPDATE {DATABASE}.ACTION.BUSINESS_EVENT SET STATUS = 'planned' WHERE EVENT_ID = %s", (risk["EVENT_ID"],))
    return {"risk_id": risk["RISK_ID"], "priority": priority, "branch": branch, "owner": risk.get("RECOMMENDED_OWNER")}


def _create_plan(conn, run_id, risk, plan_type, steps, window):
    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.ACTION_PLAN (RISK_ID, RUN_ID, PLAN_TYPE, EXECUTION_WINDOW, PRIMARY_OWNER, EXPECTED_RISK_REDUCTION_PCT)
        SELECT %s, %s, %s, %s, %s, %s
    """, (risk["RISK_ID"], run_id, plan_type, window, risk.get("RECOMMENDED_OWNER", "controller"),
          70.0 if plan_type == "mitigation" else 30.0))

    plan_rows = _query(conn, f"SELECT PLAN_ID FROM {DATABASE}.ACTION.ACTION_PLAN WHERE RISK_ID = %s AND PLAN_TYPE = %s ORDER BY PLAN_ID DESC LIMIT 1", (risk["RISK_ID"], plan_type))
    if not plan_rows:
        return
    plan_id = plan_rows[0]["PLAN_ID"]

    for seq, (action_type, autonomy, target) in enumerate(steps, start=1):
        key = _idempotency_key(risk["RISK_ID"], plan_type, action_type, seq)
        payload = {"risk_id": risk["RISK_ID"], "event_id": risk["EVENT_ID"], "branch": risk.get("ROOT_CAUSE_BRANCH"),
                   "priority": risk.get("PRIORITY"), "owner": risk.get("RECOMMENDED_OWNER")}
        initial_status = "awaiting_approval" if autonomy == "draft_and_approve" else "pending"

        _execute(conn, f"""
            INSERT INTO {DATABASE}.ACTION.ACTION_LOG
            (PLAN_ID, RUN_ID, ACTION_TYPE, ACTION_SEQ, AUTONOMY_LEVEL, TARGET_SYSTEM,
             OWNER_PERSONA, IDEMPOTENCY_KEY, PAYLOAD, STATUS, IMPACT_USD, ESCALATION_TIER)
            SELECT %s, %s, %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s
        """, (plan_id, run_id, action_type, seq, autonomy, target, risk.get("RECOMMENDED_OWNER", "controller"),
              key, _v(payload), initial_status, float(risk.get("FINANCIAL_IMPACT_USD") or 0), 1))

        if autonomy == "draft_and_approve":
            action_rows = _query(conn, f"SELECT ACTION_ID FROM {DATABASE}.ACTION.ACTION_LOG WHERE IDEMPOTENCY_KEY = %s", (key,))
            if action_rows:
                _execute(conn, f"""
                    INSERT INTO {DATABASE}.ACTION.APPROVAL_QUEUE (ACTION_ID, RUN_ID, REQUESTED_FROM, REQUEST_SUMMARY, IMPACT_USD)
                    SELECT %s, %s, %s, %s, %s
                """, (action_rows[0]["ACTION_ID"], run_id, risk.get("RECOMMENDED_OWNER"),
                      f"{action_type} for {risk.get('ROOT_CAUSE_BRANCH')} ({risk.get('PRIORITY')})",
                      float(risk.get("FINANCIAL_IMPACT_USD") or 0)))


# ============================================================
# PHASE 5: REPORT GENERATION (in-memory HTML)
# ============================================================

def phase_report(conn, run_id):
    """Build report data from ACTION tables and return HTML string."""
    events_raw = _query(conn, f"""
        SELECT e.EVENT_ID, e.EVENT_TYPE, e.ENTITY_KEY, e.SEVERITY, e.IMPACT_USD,
               e.HEADLINE, e.DESCRIPTION, e.STATUS,
               i.ROOT_CAUSE_BRANCH, i.CONFIDENCE, i.NARRATIVE AS INV_NARRATIVE, i.HYPOTHESES,
               r.RISK_SCORE, r.PRIORITY, r.CASCADE_PATH, r.RECOMMENDED_OWNER, r.NARRATIVE AS RISK_NARRATIVE
        FROM {DATABASE}.ACTION.BUSINESS_EVENT e
        LEFT JOIN {DATABASE}.ACTION.INVESTIGATION i ON i.EVENT_ID = e.EVENT_ID
        LEFT JOIN {DATABASE}.ACTION.RISK_ASSESSMENT r ON r.EVENT_ID = e.EVENT_ID
        WHERE e.RUN_ID = %s
        ORDER BY COALESCE(r.RISK_SCORE, 0) DESC, ABS(e.IMPACT_USD) DESC
        LIMIT 50
    """, (run_id,))

    # Get actions grouped by persona
    actions_raw = _query(conn, f"""
        SELECT a.ACTION_TYPE, a.OWNER_PERSONA, a.STATUS, a.AUTONOMY_LEVEL, a.IMPACT_USD,
               r.PRIORITY, r.EVENT_ID, e.HEADLINE
        FROM {DATABASE}.ACTION.ACTION_LOG a
        JOIN {DATABASE}.ACTION.ACTION_PLAN p ON p.PLAN_ID = a.PLAN_ID
        JOIN {DATABASE}.ACTION.RISK_ASSESSMENT r ON r.RISK_ID = p.RISK_ID
        JOIN {DATABASE}.ACTION.BUSINESS_EVENT e ON e.EVENT_ID = r.EVENT_ID
        WHERE a.RUN_ID = %s
        ORDER BY a.OWNER_PERSONA, a.ACTION_SEQ
    """, (run_id,))

    # Get persona details
    personas = _query(conn, f"SELECT * FROM {DATABASE}.ACTION.PERSONA_ROUTING WHERE IS_ACTIVE ORDER BY ESCALATION_TIER")

    return {
        "run_id": run_id,
        "events": events_raw,
        "actions": actions_raw,
        "personas": personas,
        "timestamp": datetime.utcnow().isoformat(),
    }

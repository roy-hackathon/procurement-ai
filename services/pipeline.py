"""
pipeline.py — Full 5-phase pipeline logic (OPTIMIZED: batch SQL, no N+1 queries).
Runs entirely via snowflake-connector-python (no CLI, no CoCo, no local files needed).
"""

import json
import hashlib
from datetime import datetime


DATABASE = "SAP_P2P_FINANCE_DEV"
SYSTEMIC_VENDOR_THRESHOLD = 5

ACTION_TABLES = [
    "AI_AUDIT_LOG", "AI_PROCUREMENT_CASE", "NOTIFICATION_OUTBOX",
    "APPROVAL_QUEUE", "ACTION_LOG", "ACTION_PLAN",
    "RISK_ASSESSMENT", "INVESTIGATION", "BUSINESS_EVENT", "WORKFLOW_RUN",
]


def reset_action_schema(conn):
    """Truncate all ACTION schema tables to start fresh."""
    for table in ACTION_TABLES:
        cur = conn.cursor()
        try:
            cur.execute(f"TRUNCATE TABLE {DATABASE}.ACTION.{table}")
        finally:
            cur.close()
    return len(ACTION_TABLES)

# Phase 4 playbooks
MITIGATION_PLAYBOOK = {
    "duplicate_ir": [("payment_hold", "auto", "internal"), ("notify_persona", "auto", "email"), ("create_incident_summary", "auto", "document_store")],
    "no_goods_receipt": [("notify_persona", "auto", "email"), ("create_incident_summary", "auto", "document_store")],
    "over_delivery": [("notify_persona", "auto", "email")],
    "price_variance": [("payment_hold", "draft_and_approve", "internal"), ("notify_persona", "auto", "email"), ("draft_sap_change_request", "draft_and_approve", "sap_draft"), ("create_incident_summary", "auto", "document_store")],
    "currency_control_gap": [("notify_persona", "auto", "email")],
    "goods_receipt_no_invoice": [("payment_hold", "draft_and_approve", "internal"), ("notify_persona", "auto", "email")],
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

BRANCH_FRIENDLY = {
    "goods_receipt_no_invoice": "Uninvoiced Goods Receipt",
    "no_goods_receipt": "Missing Goods Receipt / AP Aging",
    "price_variance": "Vendor Price Variance / Overbilling",
    "duplicate_ir": "Duplicate Invoice Receipt",
    "currency_control_gap": "Currency Control Gap",
    "payment_terms_drift": "Payment Terms Deviation",
    "over_delivery": "Over-Delivery",
    "indeterminate": "Indeterminate",
}

# 1:1 mapping: each event type belongs to exactly one persona
EVENT_TYPE_PRIMARY_OWNER = {
    "invoice_over_po": "category_manager",
    "grir_aging": "procurement_manager",
    "ap_open_item_aging": "finance_manager",
    "duplicate_invoice_receipt": "finance_manager",
    "po_invoice_currency_mismatch": "procurement_analyst",
    "unusual_payment_terms": "procurement_analyst",
}

OWNER_FRIENDLY = {
    "ap_manager": "AP Manager",
    "ap_clerk": "AP Clerk",
    "category_manager": "Category Manager",
    "controller": "Financial Controller",
    "buyer": "Buyer",
    "procurement_head": "Head of Procurement",
    "cfo": "CFO",
    "plant_manager": "Plant Manager",
    "procurement_manager": "Procurement Manager",
    "finance_manager": "Finance Manager",
    "procurement_analyst": "Procurement Analyst",
    "supply_chain_leader": "Supply Chain Leader",
    "cfo_coo": "CFO / COO",
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
    if obj is None:
        return '{}'
    if isinstance(obj, str):
        try:
            json.loads(obj)
            return obj
        except (json.JSONDecodeError, ValueError):
            return json.dumps(obj, default=str)
    return json.dumps(obj, default=str)


def _idempotency_key(*parts):
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


def next_run_id():
    return f"RUN_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"


# ============================================================
# WORKFLOW_RUN tracking
# ============================================================

def _start_workflow_run(conn, run_id):
    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.WORKFLOW_RUN
        (RUN_ID, RUN_MODE, STARTED_AT, STATUS, IS_DRY_RUN)
        SELECT %s, 'batch_ui', CURRENT_TIMESTAMP()::TEXT, 'running', 'false'
    """, (run_id,))


def _end_workflow_run(conn, run_id, status, results):
    _execute(conn, f"""
        UPDATE {DATABASE}.ACTION.WORKFLOW_RUN
        SET ENDED_AT = CURRENT_TIMESTAMP()::TEXT, STATUS = %s,
            EVENTS_DETECTED = %s, ACTIONS_PLANNED = %s, ACTIONS_EXECUTED = %s,
            IMPACT_USD = %s
        WHERE RUN_ID = %s
    """, (status, str(results.get("events_detected", 0)),
          str(results.get("actions_planned", 0)),
          str(results.get("cases_created", 0)),
          results.get("emails_sent", 0),
          run_id))


# ============================================================
# PHASE 1: DETECTION (BATCH — 2 SQL per detector)
# ============================================================

def phase_detect(conn, run_id, progress_callback=None, detectors=None, period_days=None):
    """Scan detector views and bulk-insert BUSINESS_EVENT rows (one INSERT per detector)."""
    all_detectors = _query(conn, f"SELECT * FROM {DATABASE}.ACTION.DETECTOR_REGISTRY WHERE IS_ACTIVE")

    if detectors:
        all_detectors = [d for d in all_detectors if d["EVENT_TYPE"] in detectors]

    total_inserted = 0
    total_refreshed = 0
    errors = []

    for det in all_detectors:
        det_name = det["DETECTOR_NAME"]
        view_name = f"{DATABASE}.ACTION.{det['VIEW_NAME']}"
        if progress_callback:
            progress_callback(f"Running detector: {det_name}")
        try:
            limit_clause = "ORDER BY IMPACT_USD DESC LIMIT 50" if period_days else ""

            # Batch INSERT — one SQL inserts all new events from detector view
            inserted = _execute(conn, f"""
                INSERT INTO {DATABASE}.ACTION.BUSINESS_EVENT
                (RUN_ID, EVENT_TYPE, PATTERN_CLASS, DOMAIN_PACK, ENTITY_KEY, SEVERITY, IMPACT_USD,
                 HEADLINE, DESCRIPTION, AFFECTED_KEYS, STATUS, DETECTED_AT, LAST_SEEN_AT, SEEN_COUNT, PERIOD_KEY)
                SELECT '{run_id}', d.EVENT_TYPE, 
                       COALESCE(d.PATTERN_CLASS, 'threshold_breach'),
                       COALESCE(d.DOMAIN_PACK, '{det.get("DOMAIN_PACK", "finance")}'),
                       d.ENTITY_KEY, 
                       COALESCE(d.SEVERITY, 'MEDIUM'),
                       COALESCE(d.IMPACT_USD, 0),
                       COALESCE(d.HEADLINE, '{det_name}: ' || d.ENTITY_KEY),
                       COALESCE(d.DESCRIPTION, ''),
                       COALESCE(TO_VARCHAR(d.AFFECTED_KEYS), '{{}}'),
                       'open', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), 1,
                       '{datetime.utcnow().strftime("%Y-%m")}'
                FROM ({f"SELECT * FROM {view_name} {limit_clause}"}) d
                WHERE NOT EXISTS (
                    SELECT 1 FROM {DATABASE}.ACTION.BUSINESS_EVENT e
                    WHERE e.EVENT_TYPE = d.EVENT_TYPE AND e.ENTITY_KEY = d.ENTITY_KEY
                )
            """)
            total_inserted += inserted

            # Batch UPDATE — refresh seen_count for all existing events
            refreshed = _execute(conn, f"""
                UPDATE {DATABASE}.ACTION.BUSINESS_EVENT e
                SET LAST_SEEN_AT = CURRENT_TIMESTAMP(), SEEN_COUNT = SEEN_COUNT + 1
                WHERE EXISTS (
                    SELECT 1 FROM {view_name} d
                    WHERE d.EVENT_TYPE = e.EVENT_TYPE AND d.ENTITY_KEY = e.ENTITY_KEY
                ) AND e.RUN_ID != '{run_id}'
            """)
            total_refreshed += refreshed
        except Exception as e:
            errors.append(f"{det_name}: {e}")
            if progress_callback:
                progress_callback(f"  Error: {det_name}: {e}")

    # Assign EVENT_IDs to new rows that have NULL (auto-increment may not be set up)
    _execute(conn, f"""
        MERGE INTO {DATABASE}.ACTION.BUSINESS_EVENT t
        USING (
            SELECT ENTITY_KEY, EVENT_TYPE,
                   ROW_NUMBER() OVER (ORDER BY IMPACT_USD DESC) +
                   COALESCE((SELECT MAX(EVENT_ID) FROM {DATABASE}.ACTION.BUSINESS_EVENT WHERE EVENT_ID IS NOT NULL), 0) AS NEW_ID
            FROM {DATABASE}.ACTION.BUSINESS_EVENT WHERE EVENT_ID IS NULL AND RUN_ID = '{run_id}'
        ) s ON t.ENTITY_KEY = s.ENTITY_KEY AND t.EVENT_TYPE = s.EVENT_TYPE AND t.RUN_ID = '{run_id}' AND t.EVENT_ID IS NULL
        WHEN MATCHED THEN UPDATE SET EVENT_ID = s.NEW_ID
    """)

    return {"inserted": total_inserted, "refreshed": total_refreshed, "detectors": len(all_detectors), "errors": errors}


# ============================================================
# PHASE 2: INVESTIGATION (BATCH evidence fetch)
# ============================================================

def phase_investigate(conn, run_id, limit=50, progress_callback=None):
    events = _query(conn, f"""
        SELECT e.* FROM {DATABASE}.ACTION.BUSINESS_EVENT e
        LEFT JOIN {DATABASE}.ACTION.INVESTIGATION i ON i.EVENT_ID = e.EVENT_ID
        WHERE e.STATUS = 'open' AND i.INVESTIGATION_ID IS NULL
        ORDER BY ABS(e.IMPACT_USD) DESC LIMIT %s
    """, (limit,))

    if not events:
        return []

    # Pre-fetch ALL vendor evidence in bulk (one query per hop type)
    vendor_ids = list(set(
        e["ENTITY_KEY"].split("|")[0] if "|" in e.get("ENTITY_KEY", "") else e.get("ENTITY_KEY", "")
        for e in events
    ))
    vendor_list = ",".join(f"'{v}'" for v in vendor_ids)

    if progress_callback:
        progress_callback(f"Pre-fetching evidence for {len(vendor_ids)} vendors (batch)...")

    # Bulk evidence queries
    evidence_cache = {}
    try:
        # Hop 1: PO stats per vendor
        po_stats = _query(conn, f"""
            SELECT v.VENDOR_ID, COUNT(*) AS PO_COUNT, COALESCE(SUM(po.GROSS_VALUE), 0) AS TOTAL_PO_VALUE,
                   COALESCE(AVG(po.GROSS_VALUE), 0) AS AVG_PO_VALUE
            FROM {DATABASE}.GOLD.FCT_PURCHASE_ORDERS po
            JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_SK = po.VENDOR_SK
            WHERE v.VENDOR_ID IN ({vendor_list})
            GROUP BY v.VENDOR_ID
        """)
        for r in po_stats:
            evidence_cache.setdefault(r["VENDOR_ID"], {})["hop1_po"] = r

        # Hop 2: GR vs IR
        gr_ir = _query(conn, f"""
            SELECT v.VENDOR_ID, h.EVENT_TYPE, COUNT(*) AS CNT, COALESCE(SUM(h.AMOUNT_LOCAL_CURRENCY), 0) AS TOTAL_VALUE
            FROM {DATABASE}.GOLD.FCT_PO_HISTORY h
            JOIN {DATABASE}.GOLD.FCT_PURCHASE_ORDERS po ON po.PO_ID = h.PO_ID AND po.PO_LINE = h.PO_LINE
            JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_SK = po.VENDOR_SK
            WHERE v.VENDOR_ID IN ({vendor_list})
            GROUP BY v.VENDOR_ID, h.EVENT_TYPE
        """)
        for r in gr_ir:
            vid = r["VENDOR_ID"]
            evidence_cache.setdefault(vid, {}).setdefault("hop2_gr_ir_raw", []).append(r)

        # Hop 3: Invoice stats
        inv_stats = _query(conn, f"""
            SELECT v.VENDOR_ID, COUNT(*) AS INV_COUNT, COALESCE(SUM(i.GROSS_INVOICE_AMOUNT), 0) AS TOTAL_INVOICED,
                   COALESCE(AVG(i.GROSS_INVOICE_AMOUNT), 0) AS AVG_INVOICE
            FROM {DATABASE}.GOLD.FCT_AP_INVOICES i
            JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_SK = i.VENDOR_SK
            WHERE v.VENDOR_ID IN ({vendor_list})
            GROUP BY v.VENDOR_ID
        """)
        for r in inv_stats:
            evidence_cache.setdefault(r["VENDOR_ID"], {})["hop3_invoices"] = r

        # Hop 4: Materials and plants
        mat_plant = _query(conn, f"""
            SELECT v.VENDOR_ID, m.MATERIAL_GROUP, p.PLANT_NAME, COUNT(*) AS PO_LINES
            FROM {DATABASE}.GOLD.FCT_PURCHASE_ORDERS po
            JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_SK = po.VENDOR_SK
            JOIN {DATABASE}.GOLD.DIM_MATERIAL m ON m.MATERIAL_SK = po.MATERIAL_SK
            JOIN {DATABASE}.GOLD.DIM_PLANT p ON p.PLANT_SK = po.PLANT_SK
            WHERE v.VENDOR_ID IN ({vendor_list})
            GROUP BY v.VENDOR_ID, m.MATERIAL_GROUP, p.PLANT_NAME
            ORDER BY PO_LINES DESC
        """)
        for r in mat_plant:
            evidence_cache.setdefault(r["VENDOR_ID"], {}).setdefault("hop4_materials_plants", []).append(r)

        # Hop 5: AP open items
        ap_items = _query(conn, f"""
            SELECT v.VENDOR_ID, COUNT(*) AS OPEN_ITEMS, COALESCE(SUM(a.AMOUNT_LOCAL_CURRENCY), 0) AS TOTAL_OPEN
            FROM {DATABASE}.GOLD.FCT_AP_OPEN_ITEMS a
            JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_SK = a.VENDOR_SK
            WHERE v.VENDOR_ID IN ({vendor_list})
            GROUP BY v.VENDOR_ID
        """)
        for r in ap_items:
            evidence_cache.setdefault(r["VENDOR_ID"], {})["hop5_ap_open"] = r

        # Hop 6: Vendor names
        vendor_info = _query(conn, f"""
            SELECT VENDOR_ID, VENDOR_NAME, COUNTRY FROM {DATABASE}.GOLD.DIM_VENDOR
            WHERE VENDOR_ID IN ({vendor_list})
        """)
        for r in vendor_info:
            evidence_cache.setdefault(r["VENDOR_ID"], {})["vendor_info"] = r

    except Exception as e:
        if progress_callback:
            progress_callback(f"  Evidence pre-fetch warning: {e}")

    # Now process each event using cached evidence (no more per-event queries)
    results = []
    inv_inserts = []  # collect for batch insert

    for event in events:
        if progress_callback:
            progress_callback(f"Investigating: {event.get('HEADLINE', event['EVENT_TYPE'])[:60]}")
        try:
            result = _investigate_event_cached(conn, run_id, event, evidence_cache)
            results.append(result)
            inv_inserts.append(result)
        except Exception as e:
            results.append({"event_id": event["EVENT_ID"], "branch": "error", "confidence": 0, "error": str(e)})

    # Batch update event statuses
    if inv_inserts:
        event_ids = [str(r["event_id"]) for r in inv_inserts if r.get("event_id")]
        if event_ids:
            _execute(conn, f"""
                UPDATE {DATABASE}.ACTION.BUSINESS_EVENT
                SET STATUS = 'investigating'
                WHERE EVENT_ID IN ({','.join(event_ids)})
            """)

    return results


def _investigate_event_cached(conn, run_id, event, evidence_cache):
    """Investigate using pre-fetched evidence cache (no additional queries)."""
    etype = event["EVENT_TYPE"]
    entity_key = event["ENTITY_KEY"]
    impact_usd = float(event.get("IMPACT_USD") or 0)
    vendor_id = entity_key.split("|")[0] if "|" in entity_key else entity_key

    # Pull from cache
    vendor_evidence = evidence_cache.get(vendor_id, {})
    evidence = {}

    po_data = vendor_evidence.get("hop1_po", {})
    evidence["hop1_po"] = po_data

    # Parse GR/IR from raw
    gr_ir_raw = vendor_evidence.get("hop2_gr_ir_raw", [])
    gr_value = sum(float(r["TOTAL_VALUE"]) for r in gr_ir_raw if str(r["EVENT_TYPE"]) == '1')
    ir_value = sum(float(r["TOTAL_VALUE"]) for r in gr_ir_raw if str(r["EVENT_TYPE"]) == '2')
    gr_count = sum(int(r["CNT"]) for r in gr_ir_raw if str(r["EVENT_TYPE"]) == '1')
    ir_count = sum(int(r["CNT"]) for r in gr_ir_raw if str(r["EVENT_TYPE"]) == '2')
    evidence["hop2_gr_ir"] = {"gr_value": gr_value, "ir_value": ir_value, "gr_count": gr_count, "ir_count": ir_count}

    inv_data = vendor_evidence.get("hop3_invoices", {})
    evidence["hop3_invoices"] = inv_data

    mat_plant = vendor_evidence.get("hop4_materials_plants", [])
    evidence["hop4_materials_plants"] = mat_plant
    material_groups = list(set(r["MATERIAL_GROUP"] for r in mat_plant if r.get("MATERIAL_GROUP")))
    plants_affected = list(set(r["PLANT_NAME"] for r in mat_plant if r.get("PLANT_NAME")))

    ap_data = vendor_evidence.get("hop5_ap_open", {})
    evidence["hop5_ap_open"] = ap_data

    vinfo = vendor_evidence.get("vendor_info", {})
    vendor_name = vinfo.get("VENDOR_NAME", vendor_id)
    vendor_country = vinfo.get("COUNTRY", "?")

    # Determine root cause
    branch = "indeterminate"
    confidence = 0.30
    hypotheses = []
    evidence_complete = True
    missing = None

    po_total = float(po_data.get("TOTAL_PO_VALUE", 0) or 0)
    inv_total = float(inv_data.get("TOTAL_INVOICED", 0) or 0)
    avg_po = float(po_data.get("AVG_PO_VALUE", 0) or 0)
    avg_inv = float(inv_data.get("AVG_INVOICE", 0) or 0)
    ap_open_total = float(ap_data.get("TOTAL_OPEN", 0) or 0)

    if etype == "invoice_over_po":
        if gr_value > ir_value:
            uninvoiced_gap = gr_value - ir_value
            branch = "goods_receipt_no_invoice"
            confidence = 0.85
            hypotheses = [
                {"branch": "goods_receipt_no_invoice", "score": 0.85,
                 "reason": f"GR value (${gr_value:,.0f}) EXCEEDS IR value (${ir_value:,.0f}). Vendor has NOT overbilled. Real risk: ${uninvoiced_gap:,.0f} in uninvoiced goods."},
                {"branch": "price_variance", "score": 0.25,
                 "reason": f"Apparent overbilling is aggregation mismatch: avg PO ${avg_po:,.0f} (unit) vs avg invoice ${avg_inv:,.0f} (batch)."},
                {"branch": "duplicate_ir", "score": 0.10,
                 "reason": "Cannot rule out without line-level PO-to-invoice matching (PO_ID on invoices is NULL)."},
            ]
            evidence_complete = False
            missing = "Line-level PO-to-invoice matching not available (PO_ID NULL on invoices)"
            narrative = (
                f"Five-Why Investigation for {vendor_name} ({vendor_country}):\n"
                f"1. Why does invoiced (${inv_total:,.0f}) appear to exceed PO value (${po_total:,.0f})? "
                f"PO lines are unit-level (avg ${avg_po:,.0f}), invoices are batch-level (avg ${avg_inv:,.0f}). Different aggregation levels.\n"
                f"2. Is the vendor actually overcharging? NO. GR value (${gr_value:,.0f}) > IR value (${ir_value:,.0f}). "
                f"Vendor billed LESS than goods received.\n"
                f"3. What is the real risk? ${uninvoiced_gap:,.0f} in goods received without invoice = AP accrual uncertainty.\n"
                f"4. How widespread? {len(plants_affected)} plants, {len(material_groups)} material groups affected.\n"
                f"5. Business impact: If bulk invoices arrive simultaneously, cash flow spike. "
                f"AP open items: ${ap_open_total:,.0f}. Period close at risk."
            )
        else:
            branch = "price_variance"
            confidence = 0.80
            overage = inv_total - gr_value if inv_total > gr_value else inv_total - po_total
            hypotheses = [
                {"branch": "price_variance", "score": 0.80,
                 "reason": f"IR value (${ir_value:,.0f}) exceeds GR value (${gr_value:,.0f}). Possible genuine overbilling of ${overage:,.0f}."},
                {"branch": "duplicate_ir", "score": 0.40,
                 "reason": "Multiple invoice receipts may have inflated the total."},
            ]
            narrative = (
                f"Five-Why Investigation for {vendor_name} ({vendor_country}):\n"
                f"1. Why does invoiced exceed PO? Invoice total ${inv_total:,.0f} vs PO total ${po_total:,.0f}.\n"
                f"2. Is this genuine overbilling? IR value (${ir_value:,.0f}) > GR value (${gr_value:,.0f}) — YES, possible price inflation.\n"
                f"3. How much exposure? ${overage:,.0f} potential overbilling.\n"
                f"4. Scope: {len(plants_affected)} plants, {len(material_groups)} material groups.\n"
                f"5. Impact: Payment hold recommended pending vendor reconciliation."
            )

    elif etype == "grir_aging":
        gap = gr_value - ir_value
        branch = "goods_receipt_no_invoice"
        confidence = 0.85
        hypotheses = [
            {"branch": "goods_receipt_no_invoice", "score": 0.85,
             "reason": f"{gr_count} goods receipts (${gr_value:,.0f}) vs {ir_count} invoice receipts (${ir_value:,.0f}). Gap: ${gap:,.0f} uninvoiced."},
            {"branch": "duplicate_ir", "score": 0.10,
             "reason": "Possible invoice received but not matched due to reference mismatch."},
        ]
        narrative = (
            f"Five-Why Investigation for {vendor_name} ({vendor_country}):\n"
            f"1. Why is there a GR/IR gap? {gr_count} goods receipts (${gr_value:,.0f}) vs {ir_count} invoice receipts (${ir_value:,.0f}).\n"
            f"2. How large is the uninvoiced exposure? ${gap:,.0f} in goods received without matching invoice.\n"
            f"3. Is this vendor-specific or systemic? Affects {len(plants_affected)} plants, {len(material_groups)} material groups — likely process bottleneck.\n"
            f"4. What materials are at risk? {', '.join(material_groups[:5])}.\n"
            f"5. Business impact: AP accrual uncertainty for period close. If vendor submits bulk invoice, "
            f"cash flow spike of ${gap:,.0f}. Auditors flag unmatched GR/IR beyond 30 days."
        )

    elif etype == "ap_open_item_aging":
        branch = "no_goods_receipt"
        confidence = 0.75
        hypotheses = [
            {"branch": "no_goods_receipt", "score": 0.75,
             "reason": f"Open AP item (${impact_usd:,.0f}) aging beyond payment terms. Vendor may escalate or withhold supply."},
            {"branch": "payment_terms_drift", "score": 0.20,
             "reason": "Payment terms may have shifted, creating apparent aging."},
        ]
        narrative = (
            f"Five-Why Investigation for {vendor_name} ({vendor_country}):\n"
            f"1. Why is this AP item open? Payable of ${impact_usd:,.0f} remains unpaid beyond agreed terms.\n"
            f"2. Is this isolated? Vendor has {int(ap_data.get('OPEN_ITEMS', 0) or 0)} open items "
            f"totaling ${ap_open_total:,.0f}.\n"
            f"3. What is the vendor's importance? Supplies {len(material_groups)} material groups across {len(plants_affected)} plants.\n"
            f"4. What is the risk? Vendor relationship deterioration, potential supply withholding.\n"
            f"5. Business impact: If {vendor_name} withholds supply, affects {', '.join(material_groups[:3])} "
            f"at {', '.join(p.replace('CoCoEV Plant ', '') for p in plants_affected[:3])}."
        )

    elif etype == "duplicate_invoice_receipt":
        branch = "duplicate_ir"
        confidence = 0.90
        hypotheses = [{"branch": "duplicate_ir", "score": 0.90, "reason": "Multiple invoice receipts against same PO line."}]
        narrative = f"Duplicate invoice receipt for {vendor_name}. Payment hold recommended. GR: ${gr_value:,.0f}, IR: ${ir_value:,.0f}."

    elif etype == "po_invoice_currency_mismatch":
        branch = "currency_control_gap"
        confidence = 0.95
        hypotheses = [{"branch": "currency_control_gap", "score": 0.95, "reason": "PO and invoice currencies differ."}]
        narrative = f"Currency mismatch for {vendor_name}. Three-way match cannot be evaluated until resolved."

    elif etype == "unusual_payment_terms":
        branch = "payment_terms_drift"
        confidence = 0.70
        hypotheses = [{"branch": "payment_terms_drift", "score": 0.70, "reason": "Payment terms deviate from vendor baseline."}]
        evidence_complete = False
        missing = "No corroborating signal beyond terms comparison"
        narrative = f"Payment terms anomaly for {vendor_name}. Master-data governance review needed."

    else:
        narrative = f"Event {event['EVENT_ID']} ({etype}) on {vendor_name}. No specific investigation pattern available."

    # Persist to INVESTIGATION
    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.INVESTIGATION
        (EVENT_ID, RUN_ID, ROOT_CAUSE_BRANCH, CONFIDENCE, IMPACT_USD, EVIDENCE, HYPOTHESES, EVIDENCE_COMPLETE, MISSING_EVIDENCE, NARRATIVE)
        SELECT %s, %s, %s, %s, %s, PARSE_JSON(%s), PARSE_JSON(%s), %s, %s, %s
    """, (event["EVENT_ID"], run_id, branch, confidence, impact_usd,
          _v(evidence), _v(hypotheses), evidence_complete, missing, narrative))

    # Assign INVESTIGATION_ID if NULL (no auto-increment on this account)
    _execute(conn, f"""
        UPDATE {DATABASE}.ACTION.INVESTIGATION
        SET INVESTIGATION_ID = (
            SELECT COALESCE(MAX(INVESTIGATION_ID), 0) FROM {DATABASE}.ACTION.INVESTIGATION WHERE INVESTIGATION_ID IS NOT NULL
        ) + 1
        WHERE EVENT_ID = %s AND RUN_ID = %s AND INVESTIGATION_ID IS NULL
    """, (event["EVENT_ID"], run_id))

    # Fetch the assigned ID
    inv_id_row = _query(conn, f"SELECT INVESTIGATION_ID FROM {DATABASE}.ACTION.INVESTIGATION WHERE EVENT_ID = %s AND RUN_ID = %s", (event["EVENT_ID"], run_id))
    inv_id = inv_id_row[0]["INVESTIGATION_ID"] if inv_id_row else None

    return {"event_id": event["EVENT_ID"], "investigation_id": inv_id, "branch": branch, "confidence": confidence, "impact_usd": impact_usd,
            "vendor_name": vendor_name, "narrative": narrative, "plants": plants_affected, "materials": material_groups}


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

    # Batch update event statuses
    if results:
        event_ids = [str(r["event_id"]) for r in results if r.get("event_id")]
        if event_ids:
            _execute(conn, f"""
                UPDATE {DATABASE}.ACTION.BUSINESS_EVENT
                SET STATUS = 'risk_assessed'
                WHERE EVENT_ID IN ({','.join(event_ids)})
            """)

    return results


def _assess_risk(conn, run_id, inv):
    severity_score = {"CRITICAL": 100, "HIGH": 75, "MEDIUM": 50, "LOW": 25}.get(inv.get("SEVERITY", "MEDIUM"), 40)
    impact = float(inv.get("IMPACT_USD") or 0)
    impact_score = min(100, (impact / 20000) * 100) if impact > 0 else 10
    confidence = float(inv.get("CONFIDENCE") or 0.5)
    evidence_complete = bool(inv.get("EVIDENCE_COMPLETE"))
    operational_score = 60 if not evidence_complete else 40
    branch = inv.get("ROOT_CAUSE_BRANCH") or "indeterminate"
    dependency_score = 70 if branch in ("duplicate_ir", "no_goods_receipt", "goods_receipt_no_invoice") else 40

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

    headline = inv.get("HEADLINE", "")
    narrative = (
        f"{headline} | Priority {priority} (score {composite:.0f}/100). "
        f"Root cause: {branch} (confidence {confidence:.0%}). "
        f"Primary risk category: {primary}. "
        f"Cascade path: {' -> '.join(cascade)}. "
        f"Financial exposure: ${impact:,.0f}. Assigned to: {owner}."
    )

    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.RISK_ASSESSMENT
        (EVENT_ID, INVESTIGATION_ID, RUN_ID, RISK_SCORE, PRIORITY, PRIMARY_RISK_CATEGORY,
         SECONDARY_RISK_CATEGORIES, LIKELIHOOD, IMPACT_LEVEL, FINANCIAL_IMPACT_USD,
         CASCADE_PATH, RECOMMENDED_OWNER, NARRATIVE)
        SELECT %s, %s, %s, %s, %s, %s, PARSE_JSON(%s), %s, %s, %s, PARSE_JSON(%s), %s, %s
    """, (inv["EVENT_ID"], inv["INVESTIGATION_ID"], run_id, round(composite, 2), priority, primary,
          _v(secondary), likelihood, impact_level, impact, _v(cascade), owner, narrative))

    # Assign RISK_ID if NULL
    _execute(conn, f"""
        UPDATE {DATABASE}.ACTION.RISK_ASSESSMENT
        SET RISK_ID = (
            SELECT COALESCE(MAX(RISK_ID), 0) FROM {DATABASE}.ACTION.RISK_ASSESSMENT WHERE RISK_ID IS NOT NULL
        ) + 1
        WHERE EVENT_ID = %s AND RUN_ID = %s AND RISK_ID IS NULL
    """, (inv["EVENT_ID"], run_id))

    return {"event_id": inv["EVENT_ID"], "priority": priority, "score": round(composite, 2), "owner": owner, "impact_usd": impact}


# ============================================================
# PHASE 4: ACTION PLANNING (with PAYMENT_HOLD, APPROVAL_QUEUE, SAP_CHANGE_REQUEST)
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

    # Batch update event statuses
    if results:
        event_ids = [str(r["event_id"]) for r in results if r.get("event_id")]
        if event_ids:
            _execute(conn, f"""
                UPDATE {DATABASE}.ACTION.BUSINESS_EVENT
                SET STATUS = 'planned'
                WHERE EVENT_ID IN ({','.join(event_ids)})
            """)

    return results


def _plan_for_risk(conn, run_id, risk):
    branch = risk.get("ROOT_CAUSE_BRANCH") or "indeterminate"
    priority = risk.get("PRIORITY", "P3")
    window = {"P1": "immediate", "P2": "today", "P3": "this_week", "P4": "monitor"}.get(priority, "this_week")

    mitigation_steps = MITIGATION_PLAYBOOK.get(branch, MITIGATION_PLAYBOOK["indeterminate"])
    _create_plan(conn, run_id, risk, "mitigation", mitigation_steps, window)

    if priority in ("P1", "P2") and branch in PREVENTION_PLAYBOOK:
        _create_plan(conn, run_id, risk, "prevention", PREVENTION_PLAYBOOK[branch], "this_month")

    return {"risk_id": risk["RISK_ID"], "event_id": risk["EVENT_ID"], "priority": priority, "branch": branch, "owner": risk.get("RECOMMENDED_OWNER")}


def _create_plan(conn, run_id, risk, plan_type, steps, window):
    _execute(conn, f"""
        INSERT INTO {DATABASE}.ACTION.ACTION_PLAN (RISK_ID, RUN_ID, PLAN_TYPE, EXECUTION_WINDOW, PRIMARY_OWNER, EXPECTED_RISK_REDUCTION_PCT)
        SELECT %s, %s, %s, %s, %s, %s
    """, (risk["RISK_ID"], run_id, plan_type, window, risk.get("RECOMMENDED_OWNER", "controller"),
          70.0 if plan_type == "mitigation" else 30.0))

    # Assign PLAN_ID if NULL
    _execute(conn, f"""
        UPDATE {DATABASE}.ACTION.ACTION_PLAN
        SET PLAN_ID = (
            SELECT COALESCE(MAX(PLAN_ID), 0) FROM {DATABASE}.ACTION.ACTION_PLAN WHERE PLAN_ID IS NOT NULL
        ) + 1
        WHERE RISK_ID = %s AND PLAN_TYPE = %s AND RUN_ID = %s AND PLAN_ID IS NULL
    """, (risk["RISK_ID"], plan_type, run_id))

    plan_rows = _query(conn, f"SELECT PLAN_ID FROM {DATABASE}.ACTION.ACTION_PLAN WHERE RISK_ID = %s AND PLAN_TYPE = %s AND RUN_ID = %s ORDER BY PLAN_ID DESC LIMIT 1", (risk["RISK_ID"], plan_type, run_id))
    if not plan_rows or not plan_rows[0]["PLAN_ID"]:
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

        # Populate APPROVAL_QUEUE for draft_and_approve actions
        if autonomy == "draft_and_approve":
            action_rows = _query(conn, f"SELECT ACTION_ID FROM {DATABASE}.ACTION.ACTION_LOG WHERE IDEMPOTENCY_KEY = %s", (key,))
            if action_rows:
                _execute(conn, f"""
                    INSERT INTO {DATABASE}.ACTION.APPROVAL_QUEUE (ACTION_ID, RUN_ID, REQUESTED_FROM, REQUEST_SUMMARY, IMPACT_USD)
                    SELECT %s, %s, %s, %s, %s
                """, (action_rows[0]["ACTION_ID"], run_id, risk.get("RECOMMENDED_OWNER"),
                      f"{action_type} for {risk.get('ROOT_CAUSE_BRANCH')} ({risk.get('PRIORITY')})",
                      float(risk.get("FINANCIAL_IMPACT_USD") or 0)))

        # Populate PAYMENT_HOLD for payment_hold actions
        if action_type == "payment_hold":
            entity_key = risk.get("ENTITY_KEY", "")
            vendor_id = entity_key.split("|")[0] if "|" in entity_key else entity_key
            action_id_rows = _query(conn, f"SELECT ACTION_ID FROM {DATABASE}.ACTION.ACTION_LOG WHERE IDEMPOTENCY_KEY = %s", (key,))
            hold_action_id = action_id_rows[0]["ACTION_ID"] if action_id_rows else None
            _execute(conn, f"""
                INSERT INTO {DATABASE}.ACTION.PAYMENT_HOLD
                (VENDOR_ID, HOLD_AMOUNT_USD, REASON_CODE, REASON_TEXT, PLACED_BY_ACTION_ID, PLACED_AT, IS_ACTIVE)
                SELECT %s, %s, %s, %s, %s, CURRENT_TIMESTAMP()::TEXT, %s
            """, (vendor_id,
                  str(float(risk.get("FINANCIAL_IMPACT_USD") or 0)),
                  risk.get("ROOT_CAUSE_BRANCH", "unknown"),
                  f"Auto-hold: {risk.get('ROOT_CAUSE_BRANCH')} ({risk.get('PRIORITY')})",
                  str(hold_action_id) if hold_action_id else None,
                  "true" if autonomy == "auto" else "false"))

        # Populate SAP_CHANGE_REQUEST for sap draft actions
        if action_type == "draft_sap_change_request":
            sap_action_rows = _query(conn, f"SELECT ACTION_ID FROM {DATABASE}.ACTION.ACTION_LOG WHERE IDEMPOTENCY_KEY = %s", (key,))
            sap_action_id = sap_action_rows[0]["ACTION_ID"] if sap_action_rows else None
            _execute(conn, f"""
                INSERT INTO {DATABASE}.ACTION.SAP_CHANGE_REQUEST
                (ACTION_ID, RUN_ID, SAP_OBJECT, SAP_TRANSACTION, PAYLOAD, STATUS, NOTES)
                SELECT %s, %s, %s, %s, PARSE_JSON(%s), %s, %s
            """, (str(sap_action_id) if sap_action_id else None, run_id,
                  "VENDOR_MASTER", "XK02",
                  _v({"branch": risk.get("ROOT_CAUSE_BRANCH"), "entity_key": risk.get("ENTITY_KEY"), "priority": risk.get("PRIORITY")}),
                  "draft",
                  f"Price correction for {risk.get('ROOT_CAUSE_BRANCH')}: {risk.get('ENTITY_KEY', '')}"))


# ============================================================
# PHASE 5: CASE CREATION + NOTIFICATION_OUTBOX
# ============================================================

def phase_report(conn, run_id):
    """Build report data from ACTION tables and return dict."""
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

    personas = _query(conn, f"SELECT * FROM {DATABASE}.ACTION.PERSONA_ROUTING WHERE IS_ACTIVE ORDER BY ESCALATION_TIER")

    approvals = _query(conn, f"""
        SELECT a.ACTION_TYPE, q.REQUEST_SUMMARY, q.IMPACT_USD, q.DECISION, q.REQUESTED_FROM
        FROM {DATABASE}.ACTION.APPROVAL_QUEUE q
        JOIN {DATABASE}.ACTION.ACTION_LOG a ON a.ACTION_ID = q.ACTION_ID
        WHERE q.RUN_ID = %s
    """, (run_id,))

    return {
        "run_id": run_id,
        "events": events_raw,
        "actions": actions_raw,
        "approvals": approvals,
        "personas": personas,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============================================================
# ORCHESTRATED PIPELINE WITH CASE CREATION
# ============================================================

def run_full_pipeline_with_cases(conn, run_id, progress_callback=None, limit=20, detectors=None, period_days=None):
    """Run all 5 phases and create AI_PROCUREMENT_CASE entries (batch optimized)."""
    from services.case_manager import create_case, update_case_status, audit_log, get_case_by_event

    results = {"phases": {}, "cases_created": 0, "errors": [], "emails_sent": 0}

    # Log workflow run start
    try:
        _start_workflow_run(conn, run_id)
    except Exception:
        pass

    # Check if events already exist — skip detection if so (fast path for demo)
    existing_count = _query(conn, f"SELECT COUNT(*) AS C FROM {DATABASE}.ACTION.BUSINESS_EVENT")
    has_existing_events = existing_count and int(existing_count[0].get("C", 0)) > 0

    # Phase 1: Detect
    if has_existing_events:
        if progress_callback:
            progress_callback("phase1_start", "Checking existing detected events...")
            progress_callback("phase1_done", f"Using {existing_count[0]['C']} existing events (skip re-detection)")
        results["phases"]["detect"] = {"inserted": 0, "refreshed": 0, "skipped": True}
    else:
        det_count = len(detectors) if detectors else 6
        if progress_callback:
            progress_callback("phase1_start", f"Scanning {det_count} detector views for procurement anomalies...")
        try:
            detect_result = phase_detect(conn, run_id,
                                         progress_callback=lambda msg: progress_callback("phase1_detail", msg) if progress_callback else None,
                                         detectors=detectors, period_days=period_days)
            results["phases"]["detect"] = detect_result
            if progress_callback:
                progress_callback("phase1_done", f"Detected {detect_result['inserted']} new events, refreshed {detect_result['refreshed']}")
        except Exception as e:
            results["errors"].append(f"Phase 1: {e}")
            if progress_callback:
                progress_callback("phase1_error", str(e))
            _end_workflow_run(conn, run_id, "failed", results)
            return results

    # Phase 2: Investigate (batch evidence pre-fetch)
    if progress_callback:
        progress_callback("phase2_start", "Running Five-Why evidence traversal (batch graph walk)...")
    try:
        inv_results = phase_investigate(conn, run_id, limit=limit,
                                        progress_callback=lambda msg: progress_callback("phase2_detail", msg) if progress_callback else None)
        results["phases"]["investigate"] = inv_results
        if progress_callback:
            progress_callback("phase2_done", f"Investigated {len(inv_results)} events with root cause analysis")
    except Exception as e:
        results["errors"].append(f"Phase 2: {e}")
        if progress_callback:
            progress_callback("phase2_error", str(e))
        _end_workflow_run(conn, run_id, "failed", results)
        return results

    # Phase 3: Risk Assessment
    if progress_callback:
        progress_callback("phase3_start", "Scoring risk with cascade prediction...")
    try:
        risk_results = phase_risk(conn, run_id, limit=limit,
                                  progress_callback=lambda msg: progress_callback("phase3_detail", msg) if progress_callback else None)
        results["phases"]["risk"] = risk_results
        if progress_callback:
            p1 = sum(1 for r in risk_results if r.get("priority") == "P1")
            progress_callback("phase3_done", f"Assessed {len(risk_results)} risks ({p1} P1 critical)")
    except Exception as e:
        results["errors"].append(f"Phase 3: {e}")
        if progress_callback:
            progress_callback("phase3_error", str(e))
        _end_workflow_run(conn, run_id, "failed", results)
        return results

    # Phase 4: Action Planning (populates APPROVAL_QUEUE, PAYMENT_HOLD, SAP_CHANGE_REQUEST)
    if progress_callback:
        progress_callback("phase4_start", "Selecting actions from catalog, gating money-touching decisions...")
    try:
        plan_results = phase_plan(conn, run_id, limit=limit,
                                  progress_callback=lambda msg: progress_callback("phase4_detail", msg) if progress_callback else None)
        results["phases"]["plan"] = plan_results
        if progress_callback:
            progress_callback("phase4_done", f"Created action plans for {len(plan_results)} risks")
    except Exception as e:
        results["errors"].append(f"Phase 4: {e}")
        if progress_callback:
            progress_callback("phase4_error", str(e))
        _end_workflow_run(conn, run_id, "failed", results)
        return results

    # Phase 5: Create Cases + Notifications (batch)
    if progress_callback:
        progress_callback("phase5_start", "Creating procurement cases and sending notifications...")
    try:
        case_data = _query(conn, f"""
            SELECT e.EVENT_ID, e.EVENT_TYPE, e.ENTITY_KEY, e.SEVERITY, e.IMPACT_USD, e.HEADLINE,
                   i.INVESTIGATION_ID, i.ROOT_CAUSE_BRANCH, i.CONFIDENCE, i.NARRATIVE,
                   r.RISK_ID, r.RISK_SCORE, r.PRIORITY, r.RECOMMENDED_OWNER, r.NARRATIVE AS RISK_NARRATIVE,
                   v.VENDOR_NAME, v.VENDOR_ID
            FROM {DATABASE}.ACTION.BUSINESS_EVENT e
            JOIN {DATABASE}.ACTION.INVESTIGATION i ON i.EVENT_ID = e.EVENT_ID AND i.RUN_ID = %s
            JOIN {DATABASE}.ACTION.RISK_ASSESSMENT r ON r.EVENT_ID = e.EVENT_ID AND r.RUN_ID = %s
            LEFT JOIN {DATABASE}.GOLD.DIM_VENDOR v ON v.VENDOR_ID = SPLIT_PART(e.ENTITY_KEY, '|', 1)
            WHERE e.RUN_ID = %s
            ORDER BY r.RISK_SCORE DESC
            LIMIT %s
        """, (run_id, run_id, run_id, limit))

        cases_created = 0
        persona_cases = {}  # group cases by owner for per-persona email

        for row in case_data:
            existing = get_case_by_event(row["EVENT_ID"])
            if existing:
                update_case_status(existing["CASE_ID"], "AI_INVESTIGATED",
                                   risk_level=row["SEVERITY"],
                                   risk_score=float(row.get("RISK_SCORE") or 0),
                                   investigation_id=row["INVESTIGATION_ID"],
                                   risk_id=row["RISK_ID"],
                                   root_cause=row.get("ROOT_CAUSE_BRANCH", ""),
                                   recommendation=_get_recommendation(row))
                audit_log(existing["CASE_ID"], "INVESTIGATION_COMPLETED", "AI_AGENT", "Investigation Agent",
                          f"Root cause: {BRANCH_FRIENDLY.get(row.get('ROOT_CAUSE_BRANCH', ''), row.get('ROOT_CAUSE_BRANCH', ''))}. Score: {row.get('RISK_SCORE')}")
            else:
                vendor_name = row.get("VENDOR_NAME") or row.get("ENTITY_KEY", "Unknown")
                case_id = create_case(
                    case_type=row["EVENT_TYPE"],
                    entity_id=row["ENTITY_KEY"],
                    vendor_id=row.get("VENDOR_ID") or row["ENTITY_KEY"].split("|")[0],
                    vendor_name=vendor_name,
                    headline=row.get("HEADLINE", ""),
                    financial_impact=float(row.get("IMPACT_USD") or 0),
                    severity=row["SEVERITY"],
                    event_id=row["EVENT_ID"],
                    run_id=run_id,
                    owner=EVENT_TYPE_PRIMARY_OWNER.get(row["EVENT_TYPE"], row.get("RECOMMENDED_OWNER", "procurement_manager")),
                )
                update_case_status(case_id, "AI_INVESTIGATED",
                                   risk_level=row["SEVERITY"],
                                   risk_score=float(row.get("RISK_SCORE") or 0),
                                   investigation_id=row["INVESTIGATION_ID"],
                                   risk_id=row["RISK_ID"],
                                   root_cause=row.get("ROOT_CAUSE_BRANCH", ""),
                                   recommendation=_get_recommendation(row))
                cases_created += 1

            # Group by persona for summary email
            owner = row.get("RECOMMENDED_OWNER", "procurement_manager")
            persona_cases.setdefault(owner, []).append(row)

        # Send per-persona summary emails (one email per persona, not per case)
        emails_sent = 0
        for persona, cases in persona_cases.items():
            try:
                emails_sent += _send_persona_summary_email(conn, run_id, persona, cases)
            except Exception:
                pass

        # NOTE: NOTIFICATION_OUTBOX is populated by SP_SEND_NOTIFICATION (called from _send_persona_summary_email)
        # No manual outbox insert needed here.

        GATED_BRANCHES = {"price_variance", "goods_receipt_no_invoice", "duplicate_ir"}
        awaiting_count = 0
        for row in case_data:
            branch = row.get("ROOT_CAUSE_BRANCH", "")
            if branch in GATED_BRANCHES:
                case_row = get_case_by_event(row["EVENT_ID"])
                if case_row and case_row.get("STATUS") == "AI_INVESTIGATED":
                    update_case_status(case_row["CASE_ID"], "AWAITING_DECISION")
                    audit_log(case_row["CASE_ID"], "ACTION_RECOMMENDED", "AI_AGENT", "Planning Agent",
                              f"Action requires approval: {_get_recommendation(row)}")
                    awaiting_count += 1

        # Log notifications with friendly persona names
        for persona, cases in persona_cases.items():
            friendly_persona = OWNER_FRIENDLY.get(persona, persona.replace("_", " ").title())
            for c in cases:
                case_row = get_case_by_event(c["EVENT_ID"])
                if case_row:
                    audit_log(case_row["CASE_ID"], "NOTIFICATION_SENT", "SYSTEM", "Notification Agent",
                              f"Email alert sent to {friendly_persona}")

        results["cases_created"] = cases_created
        results["emails_sent"] = emails_sent
        results["awaiting_decision"] = awaiting_count
        if progress_callback:
            progress_callback("phase5_done", f"Created {cases_created} cases, sent {emails_sent} email alerts, {awaiting_count} awaiting decision")
    except Exception as e:
        results["errors"].append(f"Phase 5: {e}")
        if progress_callback:
            progress_callback("phase5_error", str(e))

    # Summary counts for UI
    results["events_detected"] = len(results["phases"].get("detect", {}).get("events", [])) or int(results["phases"].get("detect", {}).get("inserted", 0))
    results["investigations"] = len(results["phases"].get("investigate", []))
    results["actions_planned"] = len(results["phases"].get("plan", []))

    # End workflow run
    try:
        _end_workflow_run(conn, run_id, "completed", results)
    except Exception:
        pass

    return results


def _get_recommendation(row):
    branch = row.get("ROOT_CAUSE_BRANCH", "")
    priority = row.get("PRIORITY", "P3")
    impact = float(row.get("IMPACT_USD") or 0)
    recs = {
        "duplicate_ir": f"Place Payment Hold (${impact:,.0f}) pending duplicate invoice verification",
        "price_variance": f"Place Payment Hold (${impact:,.0f}) and initiate vendor price reconciliation",
        "goods_receipt_no_invoice": "Expedite invoice collection to close GR/IR gap before period close",
        "no_goods_receipt": "Escalate to vendor for supply confirmation and payment terms review",
        "currency_control_gap": "Block payment until currency alignment confirmed with vendor master",
        "payment_terms_drift": "Review and realign payment terms with contracted baseline",
    }
    return recs.get(branch, f"Investigate and resolve {branch} ({priority})")


def _send_persona_summary_email(conn, run_id, persona, cases):
    """Send one summary email per persona using the shared FN_BUILD_ALERT_HTML template."""
    total_impact = sum(float(c.get("IMPACT_USD") or 0) for c in cases)
    impact_str = f"${total_impact/1e6:.1f}M" if total_impact >= 1e6 else f"${total_impact:,.0f}"
    case_count = len(cases)

    # Build headline from top case
    top_case = max(cases, key=lambda c: float(c.get("IMPACT_USD") or 0))
    headline = f"{case_count} procurement cases detected — {impact_str} total exposure"
    vendor = top_case.get("VENDOR_NAME") or top_case.get("ENTITY_KEY", "Multiple Vendors")
    if case_count == 1:
        headline = (top_case.get("HEADLINE") or headline)[:200]

    # Build priority from worst case
    priorities = [c.get("PRIORITY", "P3") for c in cases]
    worst_priority = min(priorities) if priorities else "P3"

    # Build actions list — deduplicate and make them strategic (not per-case repetition)
    actions = []
    vendors_affected = list(set(
        (c.get("VENDOR_NAME") or c.get("ENTITY_KEY", "Unknown"))[:30] for c in cases
    ))
    branches = list(set(c.get("ROOT_CAUSE_BRANCH", "") for c in cases))

    # Strategic actions based on root cause branches present
    if "price_variance" in branches:
        actions.append(f"Place Payment Hold on overbilling vendors ({', '.join(vendors_affected[:3])})")
        actions.append("Initiate vendor price reconciliation — compare PO rates vs invoiced amounts")
        if case_count > 1:
            actions.append(f"Qualify alternate suppliers for affected material groups ({case_count} vendors flagged)")
    if "goods_receipt_no_invoice" in branches:
        actions.append("Expedite invoice collection to close GR/IR gap before period close")
        actions.append("Set up auto-reminder when GR exceeds 14 days without matching invoice")
    if "no_goods_receipt" in branches:
        actions.append("Review blocked/disputed AP items — clear aged backlog")
        actions.append("Escalate items >90 days past due for CFO review")
    if not actions:
        actions.append("Review cases in Procurement Control Tower")
        actions.append("Approve or reject pending payment holds")
        actions.append("Escalate P1 items for immediate resolution")

    actions_str = ";".join(actions[:5])

    # Get plants and root cause
    plants = list(set(c.get("PLANT_NAME", "") for c in cases if c.get("PLANT_NAME")))
    plant_str = ", ".join(plants[:3]) if plants else "All Plants"
    root_cause = top_case.get("ROOT_CAUSE_BRANCH", "")
    root_cause_friendly = BRANCH_FRIENDLY.get(root_cause, root_cause.replace("_", " ").title())
    confidence = float(top_case.get("CONFIDENCE") or top_case.get("RISK_SCORE") or 75)

    # Use the shared SQL function to build HTML
    html_rows = _query(conn, f"""
        SELECT {DATABASE}.ACTION.FN_BUILD_ALERT_HTML(
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        ) AS HTML
    """, (worst_priority, persona, headline, total_impact, vendor, plant_str,
          root_cause_friendly, confidence, actions_str, run_id))

    if not html_rows or not html_rows[0].get("HTML"):
        return 0

    email_html = html_rows[0]["HTML"]
    owner_display = OWNER_FRIENDLY.get(persona, persona.replace("_", " ").title())
    safe_subject = f"ProcureAI Summary [{owner_display}]: {case_count} cases, {impact_str} exposure"

    # Send via SP (handles outbox + delivery)
    _execute(conn, f"""
        CALL {DATABASE}.ACTION.SP_SEND_NOTIFICATION(%s, NULL, %s, %s, %s, FALSE)
    """, (run_id, persona, safe_subject, email_html))
    return 1

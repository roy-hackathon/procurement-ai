import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import is_connected, run_query

st.set_page_config(page_title="How It Works", layout="wide", initial_sidebar_state="collapsed")
st.title("How the AI Event Detector Works")
st.caption("A plain-language explanation of the inputs, methodology, and reasoning behind the automated pipeline.")

st.divider()

# --- Section 1: What Data Goes In ---
st.header("1. What Data Goes In")

st.markdown("""
The system reads from CoCoEV's **Gold Star Schema** — a structured warehouse of procurement and finance data
organized into **Fact tables** (transactions) and **Dimension tables** (master data).

**In simple terms:** Every purchase order, invoice, goods receipt, and payment that CoCoEV processes gets recorded.
The system reads these records to look for things that don't add up.
""")

col1, col2 = st.columns(2)
with col1:
    st.subheader("Transaction Data (Facts)")
    st.markdown("""
    | What | Business Meaning |
    |------|-----------------|
    | **Purchase Orders** | What CoCoEV ordered, from whom, for how much |
    | **Invoices** | What vendors billed us |
    | **Goods Receipts** | What actually arrived at our plants |
    | **Open Payables** | What we still owe vendors |
    | **GL Entries** | The financial accounting record |
    """)
with col2:
    st.subheader("Reference Data (Dimensions)")
    st.markdown("""
    | What | Business Meaning |
    |------|-----------------|
    | **Vendors** | 28 suppliers (name, country, city) |
    | **Materials** | 25 components (Battery, Motor, Frame, etc.) |
    | **Plants** | 3 factories (Bengaluru, Pune, Chennai) |
    | **Calendar** | Dates for time-based analysis |
    | **Company Codes** | Manufacturing (1000) + Distribution (2000) |
    """)

st.divider()

# --- Section 2: What It Looks For (Detectors) ---
st.header("2. What It Looks For (Detectors)")

st.markdown("""
The system runs **6 automated checks** (called "detectors") that scan the data for anomalies.
Each detector is a pre-defined SQL query that flags transactions matching a known risk pattern.
""")

st.markdown("""
| Detector | What It Catches | Business Risk |
|----------|----------------|---------------|
| **Invoice Over PO** | Vendor billed more than what was ordered | Overpayment / price inflation |
| **GR/IR Aging** | Goods arrived but no invoice received | AP accrual uncertainty / lost invoices |
| **AP Aging** | Payments overdue beyond agreed terms | Late penalties / vendor relationship damage |
| **Duplicate Invoice** | Same PO line invoiced multiple times | Double payment risk |
| **Currency Mismatch** | PO in one currency, invoice in another | Unquantifiable exposure |
| **Payment Terms Drift** | Vendor terms changed without approval | Unauthorized cash flow impact |
""")

# Show live detector status if connected
if is_connected():
    with st.expander("Live Detector Status (from Snowflake)", expanded=False):
        detectors = run_query("""
            SELECT DETECTOR_NAME, DOMAIN_PACK, VIEW_NAME, MIN_SEVERITY_TO_ACTION, IS_ACTIVE
            FROM SAP_P2P_FINANCE_DEV.ACTION.DETECTOR_REGISTRY
            ORDER BY DETECTOR_NAME
        """)
        if detectors:
            import pandas as pd
            st.dataframe(pd.DataFrame(detectors), use_container_width=True, hide_index=True)

st.divider()

# --- Section 3: How It Investigates (Five-Why) ---
st.header("3. How It Investigates (Five-Why Reasoning)")

st.markdown("""
When the system finds an anomaly, it doesn't just report it. It **investigates** by asking "Why?" repeatedly,
following the data trail across multiple tables until it reaches a root cause.

This is called **Five-Why Analysis** — a technique used by Toyota, GE, and other manufacturers to find the
real reason behind problems (not just symptoms).
""")

st.subheader("Example: Invoice Overbilling")
st.markdown("""
```
SYMPTOM: Davis and Sons invoiced $26M against $280K in PO value

Why #1: Why does invoiced exceed PO by 93x?
  → PO lines are small ($511 avg per unit). Invoices are large ($65K avg per batch).
    They represent different aggregation levels.

Why #2: Why can't we match invoices to specific PO lines?
  → The invoice data doesn't carry the PO reference number (PO_ID is NULL).
    We can only match at vendor level, not line level.

Why #3: Are vendors actually overcharging us?
  → NO. When we check Goods Receipt amounts ($33.8M) vs Invoice Receipt amounts ($26.4M),
    the invoiced amount is LESS than what was received. No inflation.

Why #4: Then what IS the real risk?
  → The $7.4M gap between goods received and invoices = goods we received but
    haven't been billed for yet. This creates uncertainty in our AP accruals.

Why #5: Why does this matter?
  → $208M total across all vendors in uninvoiced goods. If bulk invoices arrive
    simultaneously, we face a cash flow spike. Auditors will flag this.

ROOT CAUSE: Not overbilling. It's a systemic invoice processing lag creating
            AP accrual uncertainty for period close.
```
""")

st.divider()

# --- Section 4: How It Gathers Evidence (7-Hop Traversal) ---
st.header("4. How It Gathers Evidence (Graph Traversal)")

st.markdown("""
For each investigation, the system queries **7 different data sources** in sequence,
building a complete picture before drawing conclusions:
""")

st.markdown("""
| Hop | What It Checks | Why |
|-----|---------------|-----|
| 1 | **Entity Detail** — the specific PO/invoice/item | Understand what happened |
| 2 | **3-Way Match** — GR qty vs IR qty vs PO qty | Check if quantities agree |
| 3 | **Vendor Pattern** — all events for this vendor | Isolated incident or systemic? |
| 4 | **Vendor Performance** — other POs from this vendor | Overall delivery reliability |
| 5 | **Inventory Signal** — stock movements for the material | Is supply at risk? |
| 6 | **AP Status** — open payables for this vendor | Payment relationship health |
| 7 | **GL Pattern** — financial transactions | Accounting position |

After gathering evidence, the system produces:
- **Ranked root cause candidates** (not just one guess — multiple hypotheses with confidence %)
- **Evidence graph** (which signals support or contradict each hypothesis)
- **Cascade prediction** (if this risk materializes, what else breaks?)
""")

st.divider()

# --- Section 5: Who Gets Notified (Persona Routing) ---
st.header("5. Who Gets Notified (Persona Routing)")

st.markdown("""
Every action is assigned to a specific **persona** — the person accountable for that type of issue.
Actions escalate through tiers if unresolved.
""")

if is_connected():
    personas = run_query("""
        SELECT PERSONA_CODE, DISPLAY_NAME, BUSINESS_ROLE, ESCALATION_TIER, ESCALATES_TO
        FROM SAP_P2P_FINANCE_DEV.ACTION.PERSONA_ROUTING
        WHERE IS_ACTIVE ORDER BY ESCALATION_TIER, PERSONA_CODE
    """)
    if personas:
        import pandas as pd
        df_p = pd.DataFrame(personas)
        df_p["BUSINESS_ROLE"] = df_p["BUSINESS_ROLE"].str[:100]
        st.dataframe(df_p, use_container_width=True, hide_index=True)

st.divider()

# --- Section 6: What Actions It Can Take ---
st.header("6. What Actions It Can Take")

st.markdown("""
The system uses a **fixed action catalog** — it never invents new actions.
Each action has an **autonomy level** that determines if it can execute automatically
or needs human approval.
""")

st.markdown("""
| Action | Autonomy | What Happens |
|--------|----------|--------------|
| **Notify persona** | Automatic | Sends alert to the responsible person |
| **Create incident summary** | Automatic | Documents the finding for audit trail |
| **Payment hold** | Automatic | Places a reversible hold on vendor payment |
| **SAP change request** | **Needs approval** | Drafts a change but waits for human sign-off |
| **Recommend control improvement** | Advisory | Suggests a systemic fix (prevention) |
| **Recommend vendor review** | Advisory | Flags vendor for category manager review |

**Key principle:** Actions that touch money (`payment_hold`, `draft_sap_change_request`)
are **never executed automatically**. They go to an approval queue where a human decides.
""")

st.divider()

# --- Section 7: The Output ---
st.header("7. The Output (Action Report)")

st.markdown("""
The final deliverable is an **HTML Action Report** — a standalone document that proves the system:
1. Detected specific anomalies
2. Investigated root causes with evidence
3. Assessed business risk with cascade prediction
4. Assigned specific actions to accountable personas

This is NOT a dashboard or alert — it's an **auditable action artifact** that can be shared,
reviewed, and used as evidence of automated governance.
""")

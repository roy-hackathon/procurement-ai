import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.snowflake_connection import is_connected, run_query

from components.persona import persona_selector

st.set_page_config(page_title="How Multi-Agent Works", layout="wide", initial_sidebar_state="expanded")
st.title("How Multi-Agent Works")
# persona removed
st.caption("A plain-language explanation of the multi-agent pipeline: how 5 AI agents detect, investigate, reason, and act on procurement anomalies.")

from components.sidebar_info import render_account_info
render_account_info()

st.divider()

# --- Section 1 ---
with st.expander("1. What Data Goes In", expanded=False):
    st.markdown("""
The system reads from CoCoEV's **Gold Star Schema** — a structured warehouse of procurement and finance data
organized into **Fact tables** (transactions) and **Dimension tables** (master data).

**In simple terms:** Every purchase order, invoice, goods receipt, and payment that CoCoEV processes gets recorded.
The system reads these records to look for things that don't add up.
""")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Transaction Data (Facts)**")
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
        st.markdown("**Reference Data (Dimensions)**")
        st.markdown("""
| What | Business Meaning |
|------|-----------------|
| **Vendors** | 28 suppliers (name, country, city) |
| **Materials** | 25 components (Battery, Motor, Frame, etc.) |
| **Plants** | 3 factories (Bengaluru, Pune, Chennai) |
| **Calendar** | Dates for time-based analysis |
| **Company Codes** | Manufacturing (1000) + Distribution (2000) |
""")

    st.image("assets/er-diagram.png", caption="End-to-End Data Foundation: Bronze → Silver → Gold → Semantic Layer", use_container_width=True)

# --- Section 2 ---
with st.expander("2. What It Looks For (Detectors)", expanded=False):
    st.image("assets/detectors.png", use_container_width=True)
    st.markdown("""
The system runs **6 automated checks** (called "detectors") that scan the data for anomalies.
Each detector is a pre-defined SQL query that flags transactions matching a known risk pattern.

| Detector | What It Catches | Business Risk |
|----------|----------------|---------------|
| **Invoice Over PO** | Vendor billed more than what was ordered | Overpayment / price inflation |
| **GR/IR Aging** | Goods arrived but no invoice received | AP (Accounts Payable) accrual uncertainty / lost invoices |
| **AP Aging** | Payments overdue beyond agreed terms | Late penalties / vendor relationship damage |
| **Duplicate Invoice** | Same PO (Purchase Order) line invoiced multiple times | Double payment risk |
| **Currency Mismatch** | PO in one currency, invoice in another | Unquantifiable exposure |
| **Payment Terms Drift** | Vendor terms changed without approval | Unauthorized cash flow impact |
""")

    if is_connected():
        st.markdown("**Live Detector Status (from Snowflake):**")
        detectors = run_query("""
            SELECT DETECTOR_NAME, DOMAIN_PACK, VIEW_NAME, MIN_SEVERITY_TO_ACTION, IS_ACTIVE
            FROM SAP_P2P_FINANCE_DEV.ACTION.DETECTOR_REGISTRY
            ORDER BY DETECTOR_NAME
        """)
        if detectors:
            import pandas as pd
            st.dataframe(pd.DataFrame(detectors), use_container_width=True, hide_index=True)

# --- Section 3 ---
with st.expander("3. How It Investigates (Five-Why Reasoning)", expanded=False):
    st.markdown("""
When the system finds an anomaly, it doesn't just report it. It **investigates** by asking "Why?" repeatedly,
following the data trail across multiple tables until it reaches a root cause.

This is called **Five-Why Analysis** — a technique used by Toyota, GE, and other manufacturers to find the
real reason behind problems (not just symptoms).

**Example: Invoice Overbilling**
""")
    st.code("""SYMPTOM: Davis and Sons invoiced $26M against $280K in PO value

Why #1: Why does invoiced exceed PO by 93x?
  -> PO lines are small ($511 avg per unit). Invoices are large ($65K avg per batch).
     They represent different aggregation levels.

Why #2: Why can't we match invoices to specific PO lines?
  -> The invoice data doesn't carry the PO reference number (PO_ID is NULL).
     We can only match at vendor level, not line level.

Why #3: Are vendors actually overcharging us?
  -> NO. When we check Goods Receipt amounts ($33.8M) vs Invoice Receipt amounts ($26.4M),
     the invoiced amount is LESS than what was received. No inflation.

Why #4: Then what IS the real risk?
  -> The $7.4M gap between goods received and invoices = goods we received but
     haven't been billed for yet. This creates uncertainty in our AP accruals.

Why #5: Why does this matter?
  -> $208M total across all vendors in uninvoiced goods. If bulk invoices arrive
     simultaneously, we face a cash flow spike. Auditors will flag this.

ROOT CAUSE: Not overbilling. It's a systemic invoice processing lag creating
            AP (Accounts Payable) accrual uncertainty for period close.""", language=None)

    st.image("assets/5why-analysis.png", caption="Five-Why Analysis: Digging deeper to find the real root cause, not just the symptom", use_container_width=True)

# --- Section 4 ---
with st.expander("4. How It Gathers Evidence (7-Hop Graph Traversal)", expanded=False):
    st.markdown("""
For each investigation, the system queries **7 different data sources** in sequence,
building a complete picture before drawing conclusions:

| Hop | What It Checks | Why |
|-----|---------------|-----|
| 1 | **Entity Detail** — the specific PO/invoice/item | Understand what happened |
| 2 | **3-Way Match** — GR (Goods Receipt) qty vs IR (Invoice Receipt) qty vs PO qty | Check if quantities agree |
| 3 | **Vendor Pattern** — all events for this vendor | Isolated incident or systemic? |
| 4 | **Vendor Performance** — other POs from this vendor | Overall delivery reliability |
| 5 | **Inventory Signal** — stock movements for the material | Is supply at risk? |
| 6 | **AP Status** — open payables for this vendor | Payment relationship health |
| 7 | **GL Pattern** — General Ledger financial transactions | Accounting position |

After gathering evidence, the system produces:
- **Ranked root cause candidates** (not just one guess — multiple hypotheses with confidence %)
- **Evidence graph** (which signals support or contradict each hypothesis)
- **Cascade prediction** (if this risk materializes, what else breaks at CoCoEV?)
""")

# --- Section 5 ---
with st.expander("5. Who Gets Notified (Persona Routing)", expanded=False):
    st.markdown("""
Every action is assigned to a specific **persona** — the person at CoCoEV accountable for that type of issue.
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

# --- Section 6 ---
with st.expander("6. What Actions It Can Take", expanded=False):
    st.markdown("""
The system uses a **fixed action catalog** — it never invents new actions.
Each action has an **autonomy level** that determines if it can execute automatically
or needs human approval.

| Action | Autonomy | What Happens |
|--------|----------|--------------|
| **Notify persona** | Automatic | Sends alert to the responsible CoCoEV team member |
| **Create incident summary** | Automatic | Documents the finding for audit trail |
| **Payment hold** | Needs approval | Places a reversible hold on vendor payment |
| **SAP change request** | Needs approval | Drafts a change but waits for human sign-off |
| **Recommend control improvement** | Advisory | Suggests a systemic fix (prevention) |
| **Recommend vendor review** | Advisory | Flags vendor for category manager review |

**Key principle:** Actions that touch money (payment_hold, draft_sap_change_request)
are **never executed automatically**. They go to an approval queue where a human decides.
""")

# --- Section 7 ---
with st.expander("7. The Output (Action Report)", expanded=False):
    st.markdown("""
The final deliverable is an **HTML Action Report** — a standalone document that proves the system:
1. Detected specific anomalies in CoCoEV procurement data
2. Investigated root causes with evidence (Five-Why analysis)
3. Assessed business risk with cascade prediction (which plants/product lines are affected)
4. Assigned specific actions to accountable CoCoEV personas

This is NOT a dashboard or alert — it's an **auditable action artifact** that can be shared,
reviewed, and used as evidence of automated governance.

    The report includes:
- Executive summary with KPI (Key Performance Indicator) cards
- Five-Why investigation chains with evidence
- Ranked root cause candidates with confidence percentages
- Cascade prediction (what breaks if the risk materializes)
- Action plan grouped by persona with autonomy levels
- Approval queue for money-touching decisions
""")

# --- Glossary ---
st.divider()
st.header("Glossary of Terms")
st.caption("Key procurement and finance terms used in ProcureAI, explained with CoCoEV examples.")

with st.expander("AP (Accounts Payable)", expanded=False):
    st.markdown("""
**What it means:** Money that CoCoEV owes to its suppliers for goods or services already received.

**CoCoEV example:** When Samsung SDI delivers Battery Packs to the Chennai plant, CoCoEV now has an AP obligation
to pay Samsung SDI. Until the payment is made, this appears as an "open AP item" on the books.
""")

with st.expander("PO (Purchase Order)", expanded=False):
    st.markdown("""
**What it means:** A formal document issued by CoCoEV's procurement team to a supplier, committing to buy
specific materials at an agreed price and quantity.

**CoCoEV example:** CoCoEV issues PO #4500001234 to Tata AutoComp Systems for 500 Motor Assemblies at $84.50 each,
to be delivered to the Pune plant by March 15. This is the "commitment to buy" before anything is shipped.
""")

with st.expander("GR (Goods Receipt)", expanded=False):
    st.markdown("""
**What it means:** A record confirming that materials ordered via a PO have physically arrived at a CoCoEV plant.

**CoCoEV example:** When Tata AutoComp's truck delivers 500 Motor Assemblies to the Pune plant warehouse,
the receiving team scans them in and creates a Goods Receipt (movement type 101 in SAP). This triggers
inventory to increase and creates a liability to pay the supplier.
""")

with st.expander("IR (Invoice Receipt)", expanded=False):
    st.markdown("""
**What it means:** A record of the vendor's invoice being received and matched against the PO and Goods Receipt.

**CoCoEV example:** After delivering the motors, Tata AutoComp sends an invoice for $42,250 (500 x $84.50).
CoCoEV's AP team logs this as an Invoice Receipt. The 3-way match (PO qty vs GR qty vs IR amount) must
agree before payment is released.
""")

with st.expander("GR/IR Gap (Goods Receipt / Invoice Receipt Gap)", expanded=False):
    st.markdown("""
**What it means:** The difference between goods received at CoCoEV plants and invoices received from vendors.
A large gap means CoCoEV has received materials but hasn't been billed yet (or the invoice is lost/blocked).

**CoCoEV example:** Samsung SDI has delivered $33.8M worth of Battery Packs (GR), but only $23.9M has been
invoiced (IR). The $9.9M gap means CoCoEV has goods it hasn't been billed for yet. This creates uncertainty
in financial reporting because the liability is real but not yet recorded as an invoice.
""")

with st.expander("Exposure", expanded=False):
    st.markdown("""
**What it means:** The total financial amount at risk if an anomaly is confirmed. It represents the potential
loss or unexpected cash outflow that CoCoEV could face.

**CoCoEV example:** If ProcureAI detects $25M in apparent invoice overbilling from Davis and Sons, the "exposure"
is $25M. After investigation, the real exposure might be different (e.g., the $7.4M uninvoiced goods gap
is the actual risk, not the apparent $25M overbilling which turned out to be an aggregation mismatch).
""")

with st.expander("3-Way Match", expanded=False):
    st.markdown("""
**What it means:** A control process that compares three documents before releasing payment:
1. **PO** (what was ordered)
2. **GR** (what was received)
3. **IR** (what was billed)

All three must agree on quantity and price. Discrepancies block payment and trigger investigation.

**CoCoEV example:** PO says 500 units at $84.50. GR confirms 500 arrived. Invoice says 500 at $84.50.
Match passes, payment is released. But if the invoice says 500 at $121 (43% higher), the match fails
and the item goes to the AP manager for review.
""")

with st.expander("Cascade", expanded=False):
    st.markdown("""
**What it means:** A chain reaction where one procurement problem triggers multiple downstream failures
across CoCoEV's operations.

**CoCoEV example:** If Samsung SDI (Battery Pack supplier) withholds shipments due to a payment dispute:
- Chennai plant runs out of Battery Packs within 2 weeks
- Pune plant follows within 3 weeks
- ALL three product lines (Spark Lite, Storm Pro, Glide Lite) stop production
- Dealer backorders accumulate, revenue shortfall begins

ProcureAI predicts these cascades so CoCoEV can act before the chain reaction starts.
""")

with st.expander("Persona", expanded=False):
    st.markdown("""
**What it means:** A specific role at CoCoEV that is accountable for a type of procurement action.
ProcureAI routes each finding to the right persona based on the root cause.

**CoCoEV personas:**
- **AP Manager** — handles invoice disputes, payment holds, GR/IR reconciliation
- **AP Clerk** — processes individual open items, vendor communications
- **Category Manager** — manages vendor relationships, pricing negotiations, sourcing strategy
- **Controller** — financial oversight, period close, compliance
- **CFO** — executive escalation for high-value or cross-functional issues
""")

with st.expander("Five-Why Analysis", expanded=False):
    st.markdown("""
**What it means:** A root cause investigation technique where you ask "Why?" five times in sequence,
each time digging deeper into the underlying cause rather than stopping at the surface symptom.

**CoCoEV example:**
1. Why is this invoice $25M over PO value? (Because invoices are batch-level, POs are unit-level)
2. Why can't we match at line level? (Because invoice data lacks PO reference numbers)
3. Is the vendor overcharging? (No, GR value exceeds IR value)
4. What is the real risk? ($7.4M uninvoiced goods creating AP uncertainty)
5. Why does it matter? ($208M total uninvoiced exposure, cash flow spike risk)

The technique prevents jumping to wrong conclusions (like "vendor fraud") when the real issue
is a process gap (invoice processing lag).
""")

with st.expander("Autonomy Level", expanded=False):
    st.markdown("""
**What it means:** The permission level that determines whether ProcureAI can execute an action
automatically or must wait for human approval.

**Levels:**
- **Auto** — safe actions that execute immediately (send notification, create summary)
- **Draft and Approve** — money-touching actions that go to approval queue (payment hold, SAP change request)
- **Notify Only** — advisory recommendations that inform but don't act (suggest control improvement)

**CoCoEV principle:** Any action that could freeze a vendor payment or modify SAP master data
ALWAYS requires human approval. ProcureAI never spends or blocks money on its own.
""")

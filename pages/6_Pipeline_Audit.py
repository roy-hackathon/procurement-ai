import streamlit as st
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

st.set_page_config(page_title="Pipeline Audit", layout="wide", initial_sidebar_state="collapsed")
st.title("Pipeline Logic Audit")
st.caption("Does the Streamlit pipeline follow the same reasoning path as the CoCo agent skill?")

st.divider()

# --- Overall Comparison ---
st.header("Side-by-Side: CoCo Skill vs Streamlit Pipeline")

st.markdown("""
| Phase | CoCo Agent (Full Reasoning) | Streamlit Pipeline (Deterministic) | Gap |
|-------|----------------------------|-----------------------------------|-----|
| **0. Pre-Flight** | Runs 8 checks, presents summary, gates on failures | Not implemented | Missing |
| **1. Detection** | Queries detector views, deduplicates, persists to BUSINESS_EVENT | Same logic - queries views, deduplicates, persists | Equivalent |
| **2. Investigation** | **Five-Why reasoning**: 7-hop traversal, queries multiple tables, builds evidence graph, ranks hypotheses with confidence % | **Static classification**: maps event_type to a fixed branch name + canned narrative | Major gap |
| **3. Risk Assessment** | Context-aware scoring: considers vendor position, material criticality, plant impact, cascade prediction specific to CoCoEV | **Formula-based scoring**: weighted composite (severity 25%, impact 20%, operational 20%, dependency 15%, history 10%, confidence 10%) | Partial gap |
| **4. Action Planning** | Selects from catalog with context-specific reasoning, composes detailed recommendations | **Playbook lookup**: maps branch to fixed action list (same MITIGATION_PLAYBOOK/PREVENTION_PLAYBOOK) | Logic equivalent, narrative gap |
| **5. Report** | Generates HTML with Five-Why chains, evidence graphs, cascade diagrams | Assembles data from ACTION tables, displays in Streamlit tables | Format gap only |
""")

st.divider()

# --- Detailed Gap Analysis ---
st.header("Detailed Gap Analysis")

# Phase 2 Gap
st.subheader("Phase 2: Investigation — The Biggest Gap")

col1, col2 = st.columns(2)
with col1:
    st.markdown("**CoCo Agent Does:**")
    st.markdown("""
    1. Starts from the detected event's entity key
    2. Executes **7 SQL queries** (hop-by-hop):
       - Entity detail (PO/invoice context)
       - 3-way match (GR vs IR vs PO quantities)
       - Vendor pattern (systemic or isolated?)
       - Vendor's other POs (performance history)
       - Inventory signals (stock levels)
       - AP status (open payables)
       - GL pattern (financial position)
    3. Counts supporting signals per hypothesis
    4. Produces **ranked candidates** with confidence %
    5. Builds an **evidence graph** (check/cross marks)
    6. Determines: isolated vs systemic
    7. Writes a contextual **narrative** using CoCoEV business language
    """)

with col2:
    st.markdown("**Streamlit Pipeline Does:**")
    st.markdown("""
    1. Reads the event type
    2. Maps to a **fixed branch** via if/elif:
       - `invoice_over_po` -> `price_variance` (0.85 confidence)
       - `duplicate_invoice_receipt` -> `duplicate_ir` (0.90)
       - `grir_aging` -> `goods_receipt_no_invoice` (0.85)
       - etc.
    3. No SQL queries executed for evidence
    4. No 7-hop traversal
    5. No ranked candidates from data analysis
    6. No evidence graph
    7. Canned narrative: "Invoice overbilling detected on {entity_key}"
    
    **Result:** Classification without investigation.
    The branch assignment is correct, but there's no
    reasoning trail or evidence to support it.
    """)

st.warning("""
**Why this matters for the hackathon:** The judges are looking for "multi-step reasoning" and 
"autonomous execution." The Streamlit pipeline classifies (analytics), but only the CoCo agent 
or Cortex Agent truly *reasons* (the hackathon bar).

**Mitigation:** Page 4 includes a "Run AI Five-Why Analysis" button that calls the Cortex Agent 
after the deterministic pipeline. This adds the reasoning layer back.
""")

st.divider()

# Phase 3 Gap
st.subheader("Phase 3: Risk Assessment — Partial Gap")

col1, col2 = st.columns(2)
with col1:
    st.markdown("**CoCo Agent Does:**")
    st.markdown("""
    - Considers **which specific vendor** and their position (#1-#10 by spend)
    - Knows **which material groups** are affected (Battery = CRITICAL)
    - Predicts **which plants stop** if vendor fails
    - Predicts **which product lines** (Spark/Storm/Glide) are affected
    - Uses CoCoEV-specific cascade paths
    - Example: "If Abbott-Munoz (Battery supplier) relationship 
      deteriorates → ALL 3 plants affected → ALL product lines stop"
    """)

with col2:
    st.markdown("**Streamlit Pipeline Does:**")
    st.markdown("""
    - Applies a **generic scoring formula**:
      - 25% severity weight
      - 20% financial impact (scaled to $20K benchmark)
      - 20% operational score
      - 15% dependency score
      - 10% history placeholder
      - 10% confidence
    - Maps branch to **generic cascade templates**:
      - `price_variance` -> ["margin_erosion", "budget_variance"]
      - `goods_receipt_no_invoice` -> ["ap_accrual_risk", "period_close_delay"]
    - No vendor-specific or plant-specific reasoning
    - No product-line impact prediction
    """)

st.divider()

# What Works The Same
st.subheader("What IS Equivalent")

st.success("""
**Phase 1 (Detection):** Identical logic — same detector views, same deduplication, same BUSINESS_EVENT persistence.

**Phase 4 (Action Planning):** Same MITIGATION_PLAYBOOK and PREVENTION_PLAYBOOK dictionaries. Same autonomy levels. 
Same APPROVAL_QUEUE gating for money-touching actions. Same persona owner mapping (branch -> owner).

**Phase 5 (Report):** Both assemble findings from ACTION tables. Streamlit shows in UI; CoCo produces standalone HTML.
""")

st.divider()

# --- Recommendation ---
st.header("How to Close the Gaps")

st.markdown("""
| Gap | Solution | Status |
|-----|----------|--------|
| No pre-flight checks | Add Phase 0 to pipeline page | Can add |
| No 7-hop evidence traversal | Call Cortex Agent for top events after Phase 1 | Implemented (Page 4 button) |
| No ranked hypotheses from data | Cortex Agent produces these when invoked | Implemented |
| No CoCoEV-specific cascade | Enrich Cortex Agent prompt with business context | Implemented in agent spec |
| No evidence graph | Parse Cortex Agent response, render as checklist | Can add |
| Generic scoring formula | Acceptable for deterministic path; agent adds context | OK |

**The hybrid approach:** Run the deterministic pipeline (fast, repeatable, auditable) for Phases 1/3/4/5, 
then invoke the Cortex Agent for Phase 2 reasoning on the top events. This gives you both:
- Deterministic reproducibility (same inputs -> same actions)
- LLM-powered reasoning depth (Five-Why, evidence graphs, cascade prediction)
""")

st.divider()

# --- Live Logic Trace ---
st.header("Live Logic Trace (Streamlit Pipeline)")

st.markdown("""
```
User clicks "Run Full Pipeline"
│
├── Phase 1: Detection
│   ├── Query DETECTOR_REGISTRY for active views
│   ├── For each view: SELECT * FROM VW_DETECT_*
│   ├── Deduplicate: check if ENTITY_KEY already in BUSINESS_EVENT
│   ├── INSERT new events / UPDATE seen_count for existing
│   └── Return: {inserted, refreshed, detectors, errors}
│
├── Phase 2: Investigation
│   ├── Query BUSINESS_EVENT WHERE status='open' AND no INVESTIGATION
│   ├── For each event:
│   │   ├── Match event_type to branch (if/elif)        ← NO SQL EVIDENCE QUERIES
│   │   ├── Assign fixed confidence and hypotheses       ← NO DATA-DRIVEN RANKING
│   │   └── INSERT into INVESTIGATION table
│   └── Return: [{event_id, branch, confidence}]
│
├── Phase 3: Risk Assessment
│   ├── Query INVESTIGATION WHERE no RISK_ASSESSMENT
│   ├── For each:
│   │   ├── Compute weighted composite score (formula)
│   │   ├── Assign priority (P1/P2/P3/P4)
│   │   ├── Map branch to cascade template
│   │   └── INSERT into RISK_ASSESSMENT
│   └── Return: [{event_id, priority, score, owner}]
│
├── Phase 4: Action Planning
│   ├── Query RISK_ASSESSMENT WHERE no ACTION_PLAN
│   ├── For each:
│   │   ├── Lookup MITIGATION_PLAYBOOK[branch]
│   │   ├── INSERT ACTION_PLAN + ACTION_LOG rows
│   │   ├── If P1/P2: also add PREVENTION_PLAYBOOK
│   │   └── If draft_and_approve: INSERT APPROVAL_QUEUE
│   └── Return: [{risk_id, priority, branch, owner}]
│
├── Phase 5: Report
│   ├── Query all ACTION tables for this run_id
│   ├── Group actions by persona
│   └── Display in Streamlit UI
│
└── [Optional] AI Five-Why Analysis
    ├── Build prompt from top events
    ├── Call SNOWFLAKE.CORTEX.DATA_AGENT_RUN()
    └── Display agent reasoning + evidence graph in Streamlit
```
""")

st.divider()

st.markdown("""
```
CoCo Agent Full Run (for comparison):
│
├── Phase 0: Pre-Flight (8 checks)
├── Phase 1: Detection (same as Streamlit)
│
├── Phase 2: Investigation ← KEY DIFFERENCE
│   ├── For each top event by $impact:
│   │   ├── Hop 1: SELECT * FROM FCT_PURCHASE_ORDERS WHERE vendor=X
│   │   ├── Hop 2: SELECT GR/IR counts FROM FCT_PO_HISTORY
│   │   ├── Hop 3: SELECT all events for same vendor (pattern?)
│   │   ├── Hop 4: SELECT vendor's other POs (performance)
│   │   ├── Hop 5: SELECT goods movements (inventory signal)
│   │   ├── Hop 6: SELECT AP open items (payment position)
│   │   ├── Hop 7: SELECT GL transactions (financial position)
│   │   ├── Count signals per hypothesis
│   │   ├── Rank candidates by evidence strength
│   │   └── Write contextual Five-Why narrative
│   └── Return: rich investigation with evidence graphs
│
├── Phase 3: Risk (context-aware, vendor-specific cascade)
├── Phase 4: Planning (same playbook + richer recommendations)
└── Phase 5: HTML Report (standalone artifact with Five-Why chains)
```
""")

# Vault Feedback Schemas — BidPricingHistory & BidFeedbackRecord

Specification for the two new vault tables introduced by templates.yaml v1.6.
These tables turn the vault from a static evidence library into a learning
system. To be implemented in **Phase 3 (vault)** alongside the existing
vault tables, with consumption logic built in **Phase 5 (drafting agent)**
and an ingestion workflow built in **Phase 6 (email, workflows,
notifications)**.

Both tables are **tenant-locked**. The drafting agent enforces a mandatory
`tenant_id` filter on every query and audits per query. One tenant's
feedback never feeds another tenant's drafting.

---

## BidPricingHistory

Captures the complete pricing payload, method, outcome, competitor
intelligence, and delivery cost reconciliation for every bid the tenant
submits. Feeds the score-modelling capability in the pricing_schedule
template.

### Table: `bid_pricing_history`

```sql
CREATE TABLE bid_pricing_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    bid_id UUID NOT NULL REFERENCES bids(id),
    tender_id UUID NOT NULL REFERENCES tenders(id),

    -- Bid metadata
    submission_date DATE NOT NULL,
    sector TEXT NOT NULL,
    cpv_codes TEXT[] NOT NULL,
    buyer_id UUID REFERENCES buyers(id),
    buyer_name TEXT NOT NULL,           -- denormalised for query speed
    buyer_type TEXT,                     -- council, NHS, central gov, etc.
    contract_value_estimate_gbp NUMERIC NOT NULL,
    contract_duration_months INTEGER NOT NULL,

    -- Pricing payload (the bid we made)
    our_total_price_gbp NUMERIC NOT NULL,
    our_pricing_payload JSONB NOT NULL,  -- full line items, assumptions, exclusions, tiers
    pricing_template_version TEXT,       -- schema_version from templates.yaml at time of bid

    -- Buyer method
    buyer_pricing_method TEXT NOT NULL,  -- relative, vfm_ratio, lowest_compliant, fixed_with_quality
    price_weighting_pct NUMERIC NOT NULL,
    quality_weighting_pct NUMERIC,
    social_value_weighting_pct NUMERIC,

    -- Outcome (filled when known)
    outcome TEXT NOT NULL DEFAULT 'awaiting_outcome',
                                         -- awaiting_outcome / won / lost / withdrew / no_decision
    outcome_recorded_at TIMESTAMPTZ,
    award_value_gbp NUMERIC,             -- final contract value (frameworks especially diverge from tender estimate)
    rank_if_disclosed INTEGER,           -- our rank in evaluation if buyer shares
    number_of_bidders INTEGER,

    -- Competitor intelligence (filled from award notices and debriefs)
    winning_bidder TEXT,
    winning_price_gbp NUMERIC,
    competitor_prices JSONB,             -- list of {bidder, price, rank} where disclosed
    competitor_data_source TEXT,         -- award_notice / debrief / market_intelligence / inferred

    -- Delivery cost reconciliation (filled at contract close or annually)
    delivered_cost_gbp NUMERIC,
    delivered_cost_breakdown JSONB,      -- by line item where reconcilable
    margin_realised_pct NUMERIC,
    cost_variances JSONB,                -- list of {line_item, bid_cost, actual_cost, variance_reason}
    reconciliation_date DATE,
    reconciled_by_user_id UUID REFERENCES users(id),

    -- Audit
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by_user_id UUID REFERENCES users(id),

    -- Constraints
    CONSTRAINT outcome_valid CHECK (outcome IN
        ('awaiting_outcome', 'won', 'lost', 'withdrew', 'no_decision')),
    CONSTRAINT method_valid CHECK (buyer_pricing_method IN
        ('relative', 'vfm_ratio', 'lowest_compliant', 'fixed_with_quality'))
);

-- Row-level security: tenant isolation
ALTER TABLE bid_pricing_history ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bid_pricing_history
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);

-- Indexes for the queries the agent will run
CREATE INDEX idx_bph_tenant_sector_value ON bid_pricing_history
    (tenant_id, sector, contract_value_estimate_gbp);
CREATE INDEX idx_bph_tenant_buyer ON bid_pricing_history
    (tenant_id, buyer_id);
CREATE INDEX idx_bph_tenant_outcome ON bid_pricing_history
    (tenant_id, outcome, submission_date DESC);
CREATE INDEX idx_bph_tenant_method ON bid_pricing_history
    (tenant_id, buyer_pricing_method);
```

### Queries the drafting agent will run

```sql
-- Comparable past bids by sector and value band (±50%)
SELECT * FROM bid_pricing_history
WHERE tenant_id = $1
  AND sector = $2
  AND contract_value_estimate_gbp BETWEEN $3 * 0.5 AND $3 * 1.5
  AND outcome IN ('won', 'lost')
  AND submission_date >= NOW() - INTERVAL '3 years'
ORDER BY submission_date DESC
LIMIT 10;

-- Buyer's historical pricing method pattern
SELECT buyer_pricing_method, COUNT(*) AS frequency
FROM bid_pricing_history
WHERE tenant_id = $1 AND buyer_id = $2
GROUP BY buyer_pricing_method
ORDER BY frequency DESC;

-- Win/loss rate by price position vs winning price (for win-price modelling)
SELECT
    outcome,
    AVG(our_total_price_gbp / winning_price_gbp) AS avg_price_ratio_to_winner,
    COUNT(*) AS n_bids
FROM bid_pricing_history
WHERE tenant_id = $1
  AND sector = $2
  AND winning_price_gbp IS NOT NULL
  AND outcome IN ('won', 'lost')
GROUP BY outcome;

-- Delivered cost variance for risk reserve calibration
SELECT
    AVG((delivered_cost_gbp - our_total_price_gbp) / our_total_price_gbp) AS avg_variance,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY
        (delivered_cost_gbp - our_total_price_gbp) / our_total_price_gbp) AS p90_variance
FROM bid_pricing_history
WHERE tenant_id = $1
  AND sector = $2
  AND outcome = 'won'
  AND delivered_cost_gbp IS NOT NULL;
```

---

## BidFeedbackRecord

Captures section-by-section scores, qualitative feedback, and winning-bid
intelligence from every debrief the tenant receives. This is the
calibration signal for the entire template system - it tells whether the
templates are producing 5s in practice or just look like they should.

### Table: `bid_feedback_record`

```sql
CREATE TABLE bid_feedback_record (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    bid_id UUID NOT NULL REFERENCES bids(id),
    tender_id UUID NOT NULL REFERENCES tenders(id),

    -- Debrief metadata
    debrief_received_date DATE NOT NULL,
    debrief_source_doc_id UUID REFERENCES vault_documents(id),
                                         -- the original debrief letter/email/transcript
    debrief_format TEXT,                 -- written_letter / verbal / standstill_notice
    outcome TEXT NOT NULL,               -- won / lost (only meaningful for these two)

    -- Overall result
    our_total_score NUMERIC,
    our_total_score_max NUMERIC,
    our_overall_rank INTEGER,
    winning_total_score NUMERIC,
    score_gap_to_winner NUMERIC,         -- our_total_score - winning_total_score (negative if lost)

    -- Section-by-section scores
    section_scores JSONB NOT NULL,
    /*
    Example structure:
    [
      {
        "section_id": "3.1_technical_capability",
        "template_id": "technical_capability",
        "our_score": 4,
        "max_score": 5,
        "winning_score": 5,
        "weighting_pct": 25,
        "qualitative_feedback": "Strong on methodology and named team. Weaker on contract-specific risk articulation - reviewers commented that risks felt generic.",
        "specific_gaps": ["risks_generic_not_contract_specific"],
        "what_winner_did_better": "Winner provided risk register specific to TUPE transfer with named incumbent staff retention plan"
      },
      ...
    ]
    */

    -- Winning bid intelligence (where disclosed)
    winning_bidder TEXT,
    winning_bid_differentiators JSONB,   -- list of what they did better
    winning_bid_price_gbp NUMERIC,

    -- Buyer's stated decision rationale
    decision_rationale TEXT,             -- buyer's explanation of why they chose the winner
    criteria_that_mattered_most JSONB,   -- list of criteria - often differs from ITT's stated weights
    advice_for_future_bids TEXT,         -- where buyer offers it

    -- Extracted lessons (filled by Claude after extraction, confirmed by user)
    lessons_learned JSONB,
    /*
    Example:
    [
      {
        "applies_to_template": "technical_capability",
        "issue": "Generic risks instead of contract-specific",
        "action": "Use risk_contingency template's contract_context input to seed risk identification before drafting",
        "applied_to_bids_after": ["bid-id-1", "bid-id-2"]
      }
    ]
    */

    -- Audit
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    extracted_by_agent BOOLEAN DEFAULT FALSE,
    confirmed_by_user_id UUID REFERENCES users(id),
    confirmed_at TIMESTAMPTZ,

    CONSTRAINT outcome_valid CHECK (outcome IN ('won', 'lost'))
);

-- Row-level security: tenant isolation
ALTER TABLE bid_feedback_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON bid_feedback_record
    USING (tenant_id = current_setting('app.current_tenant_id')::UUID);

-- Indexes
CREATE INDEX idx_bfr_tenant_outcome ON bid_feedback_record
    (tenant_id, outcome, debrief_received_date DESC);
CREATE INDEX idx_bfr_tenant_template ON bid_feedback_record
    USING GIN ((section_scores -> 'template_id'));
```

### Queries the drafting agent will run

```sql
-- Past feedback on the same template_id (across all bids)
SELECT
    bid_id,
    tender_id,
    debrief_received_date,
    section ->> 'our_score' AS score,
    section ->> 'qualitative_feedback' AS feedback,
    section -> 'specific_gaps' AS gaps,
    section ->> 'what_winner_did_better' AS winner_strength
FROM bid_feedback_record,
     jsonb_array_elements(section_scores) AS section
WHERE tenant_id = $1
  AND section ->> 'template_id' = $2
  AND debrief_received_date >= NOW() - INTERVAL '2 years'
ORDER BY debrief_received_date DESC
LIMIT 5;

-- Recurring low-scoring patterns (the calibration signal)
SELECT
    section ->> 'template_id' AS template_id,
    section -> 'specific_gaps' AS gap,
    COUNT(*) AS frequency
FROM bid_feedback_record,
     jsonb_array_elements(section_scores) AS section
WHERE tenant_id = $1
  AND (section ->> 'our_score')::NUMERIC < (section ->> 'max_score')::NUMERIC * 0.8
GROUP BY template_id, gap
HAVING COUNT(*) >= 3
ORDER BY frequency DESC;
-- If a gap shows up 3+ times for the same template, either the template
-- has a hole or the agent is consistently failing the same check.

-- Lessons applicable to a new draft (for surfacing to the drafting agent)
SELECT lesson
FROM bid_feedback_record,
     jsonb_array_elements(lessons_learned) AS lesson
WHERE tenant_id = $1
  AND lesson ->> 'applies_to_template' = $2;
```

---

## Ingestion workflow (Phase 6 build target)

### Result-tracking workflow

```yaml
workflow: bid_result_tracking
triggered_by: bid_submission_completed
initial_status: awaiting_outcome

states:
  awaiting_outcome:
    description: "Bid submitted; outcome not yet known"
    transitions:
      - on: outcome_recorded_by_user
        to: outcome_known
    reminders:
      - after: 4_weeks
        action: "remind user to check for outcome"
      - after: 8_weeks
        action: "escalate; debrief may need to be formally requested"

  outcome_known:
    description: "Won/lost/withdrew/no_decision recorded"
    on_entry:
      - update bid_pricing_history.outcome
      - prompt user to request debrief if outcome in (won, lost)
    transitions:
      - on: debrief_uploaded
        to: debrief_processing
      - on: user_declines_debrief
        to: closed_no_debrief

  debrief_processing:
    description: "Claude extracts structured feedback"
    actions:
      - extract section_scores from debrief document
      - extract winning_bid_differentiators
      - extract decision_rationale
      - generate proposed lessons_learned
    transitions:
      - on: extraction_complete
        to: user_confirmation_pending

  user_confirmation_pending:
    description: "User reviews extracted feedback before vault commit"
    human_action_required: true
    transitions:
      - on: user_confirms
        to: committed
      - on: user_edits_then_confirms
        to: committed

  committed:
    description: "BidFeedbackRecord written to vault; lessons available to drafting agent"
    terminal: true

  closed_no_debrief:
    description: "Outcome recorded but no debrief obtained"
    terminal: true
```

### Delivery cost reconciliation workflow

```yaml
workflow: delivery_cost_reconciliation
triggered_by:
  - contract_close_event
  - annual_reconciliation_due (for multi-year contracts)
human_initiated: true
reason: "depends on finance data outside platform"

steps:
  - prompt user to upload delivered cost data
  - reconcile against bid_pricing_history.our_pricing_payload line by line
  - calculate margin_realised_pct
  - flag variances >10% per line item for user review
  - on user confirmation, update bid_pricing_history with reconciliation fields
  - feed average variance back to risk reserve calibration in pricing template
```

---

## Vault integration points

These tables sit alongside the existing vault tables (defined in Phase 3
schema). They reference but do not modify:

- `vault_documents` (the debrief letter PDF is stored as a vault doc)
- `bids` (FK relationship)
- `tenders` (FK relationship)
- `buyers` (FK relationship)
- `tenants` (FK relationship, isolation enforcement)

Both tables are tenant-locked via Row-Level Security. The drafting agent's
DB session sets `app.current_tenant_id` at the start of every operation,
ensuring queries cannot leak across tenants even via SQL injection or
service misconfiguration.

---

## Phase 5 consumption requirements

The drafting agent must, on every bid it touches:

1. Before drafting any pricing content, query BidPricingHistory for
   comparable past bids and surface the data to the user.
2. Before drafting any quality content, query BidFeedbackRecord for past
   feedback on the same template_id (and same buyer or sector where
   possible) and surface specific gaps the user previously hit.
3. Run a "recurring low-scoring pattern" check at the bid level: if any
   draft pattern matches a recurring gap from the tenant's history, flag
   it for review before committing.
4. After bid submission, ensure the bid_result_tracking workflow is
   instantiated.

These are mandatory behaviours, validated by tests, not optional
enhancements.

---

## Commit guidance

When applying these schemas:

```
feat: add bid pricing history and feedback record tables

- bid_pricing_history: every bid's pricing payload, outcome, competitor
  data, delivery cost reconciliation
- bid_feedback_record: structured debrief data, section scores,
  winning bid intelligence, extracted lessons
- Row-level security: tenant_id isolation on both tables
- Indexes for the queries the drafting agent will run
- Phase 6 ingestion workflows: result tracking + delivery cost
  reconciliation
- Phase 5 consumption: drafting agent queries both tables before
  drafting any content

Refs: docs/bid-writing/templates.yaml schema_version 1.6
      cross_cutting_rules.vault_feedback_dependencies
```

# Schedule Module — Claude Code Implementation Spec

**Phase**: 5 (Drafting Agent) and adjacent
**Prerequisites**: Phase 3 vault, Phase 5 brand foundation, methodology_delivery template
**Inputs**: `docs/bid-writing/schedule-schema.yaml`, `docs/bid-writing/templates.yaml` v1.6

This is the marching order for Claude Code when building the schedule
module. Paste as the task brief or reference from a GitHub issue with
`@claude`.

---

## Scope (in)

A schedule subsystem comprising:

1. **Canonical schedule data model** per `schedule-schema.yaml`.
2. **Agent population logic** — the drafting agent generates an initial
   schedule from methodology inputs (contract context, mobilisation needs,
   resource curve from past contracts in BidPricingHistory).
3. **Web-based Gantt editor** — embedded in the bid editor view next to the
   §3.2 methodology_delivery response, with a full-screen pop-out for
   focused editing.
4. **PDF render via Typst** — bid-ready Gantt page(s) using the tenant
   brand foundation.
5. **Methodology template integration** — the schedule data feeds the
   methodology_delivery template's slots directly; no duplication.
6. **Baseline and edit history** — snapshots and replayable audit log.

## Scope (out — do not build)

- Export to MS Project (.mpp, .xml), Primavera P6 (.xer), Asta
  Powerproject, or CSV
- Re-import from any external scheduling tool
- Schedule sharing as an interactive viewer with the buyer (buyer
  receives the PDF in the bid pack only)
- Multi-user concurrent editing (single-user editing only for v1)
- Resource cost optimisation or scenario modelling
- Earned-value or progress-tracking analytics beyond completion_pct field

## Architecture

### Data model

Implement the schema in `schedule-schema.yaml` as a Pydantic model
hierarchy. The root `Schedule` model contains workstreams, phases, tasks,
milestones, dependencies, resources, critical_path, baselines, display
config, and edit_history.

Persist to Postgres with one table per top-level collection (or use
JSONB where the nested structure is naturally cohesive — workstreams,
phases, and display are good JSONB candidates; tasks, dependencies,
resources are better as separate tables for query-ability).

Every table tenant-locked via Row-Level Security (see
PROJECT.md §10 tenant isolation).

### Critical path calculation

Implement using standard CPM algorithm:

1. Forward pass: compute earliest start and earliest finish for each task,
   respecting dependencies.
2. Backward pass: compute latest start and latest finish from project end
   working back.
3. Float = latest_start - earliest_start.
4. Critical path = tasks where float == 0.

Recalculate on every edit. The result populates `schedule.critical_path`
and the `on_critical_path` / `float_weeks` fields on each task.

### Agent population logic

`services/scheduling/agent_population.py`:

```python
def generate_initial_schedule(
    db: Session,
    bid_id: UUID,
    methodology_inputs: MethodologyInputs,
    tenant_id: UUID,
) -> Schedule:
    """
    Generate the initial schedule from methodology inputs.

    Inputs the agent considers:
    - Contract duration, value, start date (from tender extraction)
    - Workstream taxonomy (from tenant template or sector default)
    - Mobilisation pattern (from BidPricingHistory if comparable past
      contracts exist; otherwise from sector default)
    - Resource curve (from BidPricingHistory comparable contracts)
    - Mandatory contractual milestones (from tender requirements)
    - Buyer's stated milestones from the ITT

    The agent prompts Claude to produce a structured schedule matching
    schedule-schema.yaml, with:
    - At least 4 phases per workstream
    - At least 4 mobilisation tasks in weeks 1-4
    - All decision gates from the tender mapped to milestones
    - Exit transition phase in final 3-6 months
    - Resource curve consistent with FTE patterns from past comparable
      contracts

    No invention - every task has a vault_citation linking to evidence of
    capability to deliver it.
    """
```

### Web editor

Library: an open-source Gantt primitive. Realistic options:

- **frappe-gantt** (MIT, mature, minimal) — fastest path, simpler UX,
  significant custom work for brand styling, multi-workstream lanes,
  dependency editing
- **vis-timeline** (Apache 2.0, mature) — broader timeline visualisation,
  drag-edit works, dependency support is limited and would need extension
- **dhx-gantt free tier** — capable but the free tier has feature gaps
  that may force the build to skirt features users want

Recommendation: **frappe-gantt** as the base, with significant custom
work on top:

- Multi-workstream lanes (frappe-gantt supports single-level grouping;
  workstreams + phases is two-level — extend the rendering)
- Drag-to-edit dependencies of all four types (frappe-gantt has basic
  dependency lines; needs extension)
- Resource histogram below the Gantt (calculated from resource_curve;
  render with D3 or a simple bar chart library)
- Brand foundation styling (colours from tenant palette, typography from
  tenant pair) — override frappe-gantt's default CSS
- Full-screen pop-out (route-level component that takes the full viewport)
- Day/week/month zoom (frappe-gantt has zoom; ensure labels match
  granularity)
- Critical path highlight (override bar styling for critical tasks)
- Baseline overlay toggle (render baseline bars in a muted brand colour
  behind current bars when display.show_baseline_overlay is true)

The editor is React (matches the rest of the dashboard from Phase 2).
Build it as a self-contained component that takes a Schedule object as
prop, emits edit events, and round-trips the full Schedule on save.

### PDF render via Typst

`services/scheduling/typst_render.py`:

```python
def render_schedule_pdf(
    db: Session,
    schedule_id: UUID,
    tenant_id: UUID,
) -> Path:
    """
    Render the schedule to a bid-ready PDF using Typst.

    Process:
    1. Load schedule and tenant brand foundation.
    2. Generate Typst source from a parameterised template:
       - Page setup per display.pdf_page_size (A3 landscape default)
       - Title block from display.title_block
       - Gantt chart drawn with Typst's primitives (rectangles, lines,
         text) - one row per task, sorted by workstream then phase then
         task sort_order
       - Workstream lanes with subtle background bands using tenant
         palette
       - Task bars filled with phase colour_treatment from brand
         foundation
       - Critical path tasks highlighted with thicker stroke or accent
         colour
       - Dependencies drawn as connector lines (S-curves for clarity)
       - Milestones drawn as diamond markers
       - Decision gates with named gate criteria as footnote markers
       - Resource histogram below the Gantt
       - Critical path summary as text block at bottom
    3. Invoke Typst compiler.
    4. Return path to generated PDF.

    The Typst template is parameterised on the tenant brand foundation,
    so two tenants produce visually distinct Gantt PDFs. The Typst
    template itself is held in vault as part of the brand foundation
    (tenant-locked).
    """
```

Why Typst not Playwright:

The user has confirmed Typst over Playwright. The reasoning:

- Typographic control is materially better; brand-foundation typography
  pair renders consistently to PDF
- Vector output, infinitely scalable, sharp at any print size
- Same engine as case study generator, so the visual subsystem is unified
- Output quality ceiling is higher; matches "appearance is paramount"
- The cost is a more involved Typst template; this is a one-off per
  tenant brand foundation, reused for every schedule render

The Typst template is built alongside the case study templates as part
of the tenant brand foundation onboarding (Phase 5).

### Methodology template integration

`services/drafting/methodology_population.py`:

When the drafting agent populates the methodology_delivery template
(§3.2), it queries the bid's Schedule object and projects its data into
the template slots:

```python
def populate_methodology_from_schedule(
    schedule: Schedule,
    methodology_response: MethodologyDeliveryResponse,
) -> MethodologyDeliveryResponse:
    """
    Project schedule data into the methodology_delivery template.

    Mappings (per schedule-schema.yaml cross-references):
    - project_plan.milestones        <- schedule.milestones
    - project_plan.decision_gates    <- milestones where type=decision_gate
    - project_plan.critical_path_summary <- schedule.critical_path
    - mobilisation.mobilisation_weeks <- tasks in first phase, weeks 1-4
    - resource_plan.phases           <- schedule.phases + resources
    - resource_plan.resource_curve_summary <- derived from resource_curve
    - exit_transition                <- last phase named "Exit" or similar

    The methodology_delivery template's slots are populated DIRECTLY
    from the schedule. The drafting agent does not re-invent milestones
    in prose. One source, multiple views.

    The reverse direction also holds: if a user edits the schedule, the
    methodology response is automatically regenerated to stay consistent.
    Cross-section consistency check (see templates.yaml v1.6 cross-cutting
    rules) validates that schedule and methodology agree.
    """
```

### Edit history and baselines

Every edit operation emits an `EditHistory` record before the change is
committed:

```python
def apply_edit(
    db: Session,
    schedule_id: UUID,
    operation: EditOperation,
    user_id: UUID,
) -> Schedule:
    # 1. Load current schedule
    # 2. Capture previous_state of affected object
    # 3. Apply the operation
    # 4. Recalculate critical_path
    # 5. Validate against schedule_integrity rules
    # 6. Capture new_state of affected object
    # 7. Append EditHistory record
    # 8. Commit transaction
    # 9. Return updated schedule
```

Baselines are taken explicitly by the user (button in the editor) or
automatically at agent_generated, bid_submitted, contract_start states.
A baseline is an immutable full snapshot.

## File layout

```
tender-agent/
  src/tender_agent/
    services/
      scheduling/
        __init__.py
        models.py                  # Pydantic models matching schedule-schema.yaml
        agent_population.py        # initial schedule generation
        critical_path.py           # CPM algorithm
        edit_operations.py         # apply edits with history capture
        baseline.py                # snapshot management
        typst_render.py            # PDF render
        validators.py              # schedule_integrity + methodology_alignment
        service.py                 # public API
      drafting/
        methodology_population.py  # bridge schedule -> methodology template
    models.py                      # add Schedule, Workstream, Phase, Task,
                                   # Dependency, Resource, Milestone, Baseline,
                                   # EditHistory tables
    api/
      schedules.py                 # REST endpoints for CRUD + edit operations
  tender-agent-dashboard/
    components/
      schedule/
        GanttEditor.tsx            # frappe-gantt wrapper, brand-styled
        WorkstreamLanes.tsx        # multi-lane extension
        DependencyEditor.tsx       # drag-to-create dependencies
        ResourceHistogram.tsx      # FTE per week chart
        BaselineOverlay.tsx        # show drift vs baseline
        FullScreenPopOut.tsx       # route-level full-viewport editor
      bid_editor/
        MethodologyTab.tsx         # embeds GanttEditor next to §3.2 response
  tests/
    services/scheduling/
      test_models.py
      test_critical_path.py
      test_edit_operations.py
      test_agent_population.py
      test_typst_render.py
      fixtures/
        schedule_minimal.json
        schedule_complex.json
```

## Hard rules

1. **Tenant isolation**: every schedule operation takes tenant_id; queries
   filter on it; RLS enforces at DB level. Cross-tenant leak tests included.
2. **No invention**: the agent populates tasks only when a corresponding
   vault citation exists for the capability claimed. If no citation,
   the task is flagged as `unfilled` for human review.
3. **Critical path always current**: recalculated on every edit before
   commit. Never serve stale critical path data.
4. **Brand foundation required for PDF render**: blocking validation —
   cannot produce bid-ready PDF without tenant brand foundation.
5. **No external format integration**: no MPP, XER, P6, Asta, CSV. Period.
6. **Single-user editing for v1**: optimistic locking with `updated_at`
   version check. If two users edit, second one gets a conflict and must
   reload. No CRDT, no real-time sync.

## Testing

- Unit tests for the CPM algorithm with known-good fixtures (small
  schedules with hand-calculated critical paths).
- Edit operation tests covering each operation type, with EditHistory
  verification.
- Methodology population tests: given a schedule, verify the methodology
  template slots are populated correctly.
- Typst render tests: render a fixture schedule, verify the output PDF
  exists and parses, snapshot-test the SVG output for visual regression.
- Tenant isolation tests: verify queries cannot return another tenant's
  schedule even with malformed parameters.
- Validation tests: verify each schedule_integrity rule fails appropriately
  on malformed inputs.

## Acceptance criteria

- [ ] Schedule data model implemented per schema with all tables tenant-
      locked and RLS enforced.
- [ ] Critical path calculation correct on all fixture schedules.
- [ ] Agent can generate an initial schedule from methodology inputs that
      satisfies methodology_template_alignment validation.
- [ ] Web Gantt editor renders schedules with multi-workstream lanes,
      brand-foundation styling, critical path highlight, resource histogram.
- [ ] Editor supports drag-edit on tasks (move and resize), drag-create
      dependencies (all four types), resource assignment, milestone
      addition.
- [ ] Full-screen pop-out works from the embedded view.
- [ ] Typst PDF render produces bid-ready output using tenant brand
      foundation; output matches a golden fixture (allowing for timestamp
      diffs).
- [ ] Methodology template auto-populates from schedule and stays
      consistent across edits.
- [ ] Edit history captures every operation with previous/new state.
- [ ] Baselines can be taken and overlaid.
- [ ] Tenant isolation enforced; cross-tenant leak tests pass.
- [ ] Lint clean (ruff), type-clean (mypy strict on scheduling module),
      90%+ test coverage.

## Out of scope (do NOT do)

- Export to MS Project, P6, Asta, CSV
- Re-import from any external scheduling tool
- Interactive viewer shared with buyer
- Multi-user concurrent editing
- Resource cost optimisation
- Earned-value / progress analytics
- Schedule templates beyond the agent-generated initial schedule

If users push for these later, raise as separate issues with their own
scoping. Do not expand this brief.

## When you finish

Open a PR titled **"Phase 5: Schedule module with embedded Gantt editor
and Typst PDF render"**. Include:

1. Test coverage report.
2. End-to-end demo: fixture tender → agent-generated schedule →
   user edits via web editor → methodology template auto-populated →
   PDF rendered.
3. Screenshots of: embedded editor, full-screen pop-out, generated PDF.
4. Updated PROJECT.md noting Phase 5 schedule subsystem status.

Pause for human review before merging.

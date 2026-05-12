# Observability spec

What we want to know about a running Tender Agent and how we'll know it. This
spec covers metrics, dashboards, alarms, log retention, and where Sentry
plays. The actual CloudWatch dashboard JSON + alarm Terraform aren't in the
T6-prep package — they're per-env tuning that needs an operator decision on
alarm targets (SNS / PagerDuty / email).

## What matters

The agent has four concerns. Each gets its own dashboard.

### Ingestion (per source: FTS, CF, PCS, S2W, NI)

- **Poll success rate** — fraction of `ingest.complete` events with
  `status="ok"`, per source, over the trailing hour.
- **Poll cadence** — time between consecutive `ingest.start` events. Should
  match `POLL_INTERVAL_MINUTES` ± jitter.
- **Records fetched per poll** — `fetched` field on `ingest.complete`. Sudden
  drop to zero across all sources usually means egress is broken.
- **Adapter failure rate** — count of `<src>.fetch_failed` events per hour.
  Source-specific 4xx/5xx breakdowns when available.
- **Normalisation failure rate** — count of `<src>.normalise_failed` events.
  Above zero means an OCDS shape drift the normaliser isn't handling.

### Extraction (Claude requirements pipeline)

- **Extraction latency** — wall-clock between `requirements.requested` and
  the corresponding `requirements.extracted` or `requirements.api_error`.
  p50 / p95 / p99.
- **Extraction success rate** — fraction of extraction attempts that produce
  a valid TenderRequirements row (not `requirements.skipped_no_api_key`,
  `requirements.no_content`, `requirements.api_error`, or
  `requirements.parse_failed`).
- **Anthropic spend proxy** — count of successful extractions × the model
  cost we estimate per call. Real billing is at the API key level; this is a
  fast-path daily indicator.
- **Hallucination rate** — produced by `scripts/validate_extractor.py` when
  the operator runs it. Target < 2% per docs/extractor-validation.md.

### Push delivery

- **Subscribers** — `count(push_subscriptions)`, per filter and catch-all.
- **Delivery rate** — count of `push.dispatch_complete` events × `sent /
  total`, trailing hour.
- **Dispatch failures** — count of `push.send_failed` and `push.send_error`
  events. Status-code breakdown via the structured field.
- **Dead-endpoint churn** — count of `push.endpoint_gone` events per day.
  Sustained high churn means users are uninstalling rather than
  unsubscribing; non-fatal but useful signal.
- **`last_used_at` lag** — for each subscriber, time since last successful
  push. Stale entries (> 60d) without `endpoint_gone` are candidates for a
  re-subscription prompt later.

### Infrastructure

- **ALB request volume + error rates** — `RequestCount`,
  `HTTPCode_Target_5XX_Count`, `TargetResponseTime` p95.
- **ECS task counts** — `RunningCount` per service. Should equal
  `desired_count`. Drops below indicate health-check failures or capacity
  problems.
- **ECS CPU + memory** — per service, average across tasks. Target < 70%
  steady state; > 85% sustained is a scaling signal.
- **RDS** —
  - `CPUUtilization` (alarm at 80%)
  - `FreeStorageSpace` (alarm at 80% used)
  - `DatabaseConnections` (alarm at 80% of pool ceiling)
  - `ReadIOPS` / `WriteIOPS` trend
  - `ReadLatency` / `WriteLatency` p99
- **NAT Gateway** — `BytesOutToDestination` (proxy for outbound egress
  spend) per AZ.
- **CloudWatch log ingest** rate per log group — proxy for log spend.

## Dashboards

Four CloudWatch dashboards, one per concern. Each dashboard is environment-
scoped — staging and prod don't share dashboards.

### Ingestion dashboard

```
┌────────────────────────────┬────────────────────────────┐
│  Poll success rate         │  Records fetched per poll  │
│  (line, per source, 1h)    │  (line, per source, 1h)    │
├────────────────────────────┼────────────────────────────┤
│  Adapter failures          │  Normalisation failures    │
│  (line, per source, 24h)   │  (line, per source, 24h)   │
├────────────────────────────┼────────────────────────────┤
│  ingest.complete events    │  ingest.failed events      │
│  (log metric, 24h)         │  (log metric, 24h)         │
└────────────────────────────┴────────────────────────────┘
```

### Extraction dashboard

```
┌────────────────────────────┬────────────────────────────┐
│  Extraction latency        │  Extraction success rate   │
│  (p50/p95/p99, 6h)         │  (line, 6h)                │
├────────────────────────────┼────────────────────────────┤
│  Calls today               │  Failures by type          │
│  (single value, today)     │  (stacked bar, 24h)        │
└────────────────────────────┴────────────────────────────┘
```

### Push dashboard

```
┌────────────────────────────┬────────────────────────────┐
│  Subscribers (now)         │  Delivery rate             │
│  (single value)            │  (line, 1h)                │
├────────────────────────────┼────────────────────────────┤
│  Dispatch failures by code │  Endpoint-gone count       │
│  (stacked bar, 24h)        │  (line, 7d)                │
└────────────────────────────┴────────────────────────────┘
```

### Infrastructure dashboard

```
┌────────────────────────────┬────────────────────────────┐
│  ALB requests + 5xx        │  Target response time p95  │
│  (line, 6h)                │  (line, 6h)                │
├────────────────────────────┼────────────────────────────┤
│  ECS running count         │  ECS CPU + memory          │
│  (line per service, 24h)   │  (line per service, 24h)   │
├────────────────────────────┼────────────────────────────┤
│  RDS CPU + connections     │  RDS storage free          │
│  (line, 24h)               │  (line, 24h)               │
└────────────────────────────┴────────────────────────────┘
```

## Alarms

All alarms route to an SNS topic per env (`tender-agent-<env>-alerts`).
Operator wires that topic to email + PagerDuty / Slack as their on-call
flow dictates. The thresholds below are starting points — expect to tune in
the first month against real noise.

| Alarm | Condition | Severity | Notes |
|---|---|---|---|
| `poll.consecutive_failures` | 2+ consecutive `ingest.complete` events with `status="error"` for the same source | Warning | One bad cycle is noise; two means the upstream changed shape. |
| `api.health_5xx_rate` | ALB `HTTPCode_Target_5XX_Count` / `RequestCount` > 1% over 5 min | Page | The `/health` route should never 5xx in steady state. |
| `api.unhealthy_tasks` | ECS service `RunningCount` < `DesiredCount` for > 5 min | Page | Task can't pass health checks. |
| `push.delivery_rate_low` | `push.dispatch_complete.sent / total` < 90% over 1 hour | Warning | Some 410s are normal; sustained low rate means our subs DB has stale records or the push provider is sad. |
| `rds.cpu_high` | RDS `CPUUtilization` > 80% over 10 min | Warning | Persistent → scale up instance class. |
| `rds.storage_low` | RDS `FreeStorageSpace` < 20% of `allocated_storage` | Warning | Storage autoscaling raises the ceiling but doesn't fix the trend. |
| `rds.connections_high` | RDS `DatabaseConnections` > 80% of effective ceiling | Warning | Probably a connection leak in the app; check `db.session` close calls. |
| `requirements.parse_failure_burst` | More than 5 `requirements.parse_failed` events in 30 min | Warning | Claude returned bad JSON repeatedly; might mean a model bug or prompt regression. |
| `extractor.hallucination_rate` | `validate_extractor.py` exit code != 0 on the nightly run | Warning | Run the script as a scheduled GHA / EventBridge → Lambda once the validation corpus stabilises. |
| `cost.budget` | AWS Budgets monthly forecast > 1.5× expected | Warning | Configured in AWS Budgets, not CloudWatch. Email-only. |

## Log retention

Per the data-handling guidance in `tender-agent/CLAUDE.md` §7 and PROJECT.md
§8:

| Log source | Retention | Storage |
|---|---|---|
| Application logs (ECS → CloudWatch) | **90 days** | CloudWatch log groups (`log_retention_days = 90` is the module default; envs can override). |
| RDS Postgres logs | 90 days | CloudWatch via `enabled_cloudwatch_logs_exports`. |
| Audit log in `poll_runs` | **7 years** | Postgres. Per-row, never purged. Annual snapshot of the table → S3 documents bucket Glacier for cold archive. |
| `filter_matches` history | 7 years | Same as audit — the record of "did we ever alert on this tender" is a compliance artefact. |
| `tender_document_files` blobs | Lifetime of the contract + 7 years | S3 documents bucket — Glacier transition after 1y. |
| Debug screenshots (Phase 4) | 90 days | S3 debug bucket — lifecycle expires automatically. |

90-day app log retention is the default in `modules/ecs_service`. Bumping
retention is a one-tfvar change; lowering it is a deliberate cost decision
that loses incident-debugging surface area for older issues.

## Where Sentry fits

CloudWatch covers metrics + log events. **Sentry covers errors** — exception
traces with breadcrumbs, the stuff that's hard to grep out of structured logs.

Plug points:

- **API**: `sentry_sdk.init(...)` in `tender_agent.main` with a `SENTRY_DSN`
  read from settings + Secrets Manager. `FastApiIntegration` for request
  context.
- **Worker**: same `sentry_sdk.init(...)` at the top of `scheduler.py`.
- **What to send**: anything caught by the top-level `except` in
  `services/ingestion.poll_source`, `services/requirements_extractor.extract_requirements`,
  `services/push.send_to_subscribers`. Use
  `sentry_sdk.capture_exception(exc)` from within the existing `except`
  branches; the structured log is unchanged.
- **What to NOT send**: anything `services/push._send_one` handles as a
  routine WebPush failure (410, 404). Those are operational, not error
  conditions.

The DSN goes in Secrets Manager (`<env>/sentry-dsn`) and is injected into
the task definition same as the Anthropic key. Sentry's own free tier covers
the volume we'd expect at this stage.

CloudWatch metrics + alarms tell us **something is wrong with the system**;
Sentry traces tell us **what threw and where**. The two are
complementary — we want both.

## Implementation status

The Terraform package in this branch (T6-prep) ships:

- CloudWatch log groups with the 90-day retention default.
- Container Insights enabled on the ECS cluster (the cheap, agentless way
  to get the per-service CPU + memory metrics).
- Performance Insights enabled on RDS (7-day retention; free tier).
- RDS Enhanced Monitoring at 60-second granularity.

The Terraform does **not** ship the dashboards or the alarms — those land in
a follow-up once we know the alert target (SNS topic, email list, PagerDuty
service key). The starting alarm thresholds above are the inputs for that
follow-up.

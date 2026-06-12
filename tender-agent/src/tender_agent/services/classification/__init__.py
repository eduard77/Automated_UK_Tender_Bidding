"""Cross-source sector classification — the normalisation spine (Phase 3a).

Every tender from every source is AI-classified into a fixed sector/category
scheme at ingest, so the dashboard can filter by *sector* (which every tender
has, via title + description) instead of CPV (which most sources don't supply
cleanly). CPV is demoted to a derived/secondary signal.

Public surface:
  - taxonomy: the fixed value lists + validation + the cached system prompt.
  - classifier: classify one tender (Haiku 4.5), validate, persist.
  - backfill: one-time Batch-API job over the existing unclassified pool.
"""

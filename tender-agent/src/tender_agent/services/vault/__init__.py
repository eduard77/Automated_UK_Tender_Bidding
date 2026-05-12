"""Document vault — Phase 3.

What goes in here:
- claims_schemas: per-document-type Pydantic models for the structured "claims"
  blob that's queryable by the re-validation engine.
- extractors/: one module per document type. Each takes (text, title) and
  returns a parsed claims model (or raises ClaimsExtractionError).
- classifier: routes a new upload to a category + the right extractor.
- matcher: the re-validation engine — given a tender requirement, ranks vault
  documents and returns a verdict.
- storage: blob storage abstraction (LocalVaultStorage in dev, S3 in prod).
- embeddings: text → vector(1536) abstraction. Null/Hash defaults; real
  providers wired in production.

See PROJECT.md §5.4 for the design spec and docs/vault.md for the operator
walkthrough.
"""
from __future__ import annotations

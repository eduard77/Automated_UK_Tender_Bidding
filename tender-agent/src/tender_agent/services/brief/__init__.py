"""Bid-brief generation pipeline.

Three pieces:
- `document_extractor` extracts text from a stored TenderDocumentFile.
- `content_store` persists the extracted text as TenderDocumentContent rows
  keyed by sha256 + extractor_version, so identical content is extracted ONCE
  and reused forever — even across tenders.
- `llm_client` + `brief_generator` turn the stored content into a validated
  recommendation-led brief (bid / no-bid / conditional, key risks first).
"""

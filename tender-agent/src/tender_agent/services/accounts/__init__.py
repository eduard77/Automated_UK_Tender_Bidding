"""Account services — signup/login, sessions, entitlement gating.

Search stays anonymous; nothing here is on the request path for /search. The
gate runs server-side on /tenders/{id}/brief and on the document content
endpoints (see api/tender_brief.py + the document-download path).
"""

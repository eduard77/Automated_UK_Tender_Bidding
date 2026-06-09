"""Email integration (Phase 6 §5.8).

Connect an inbox over OAuth (delegated READ-ONLY access — never stored
passwords), watch for tender emails identified by an EXACT tender reference in
the subject line, file the email + attachments against the matching tender
(reusing the existing document storage + dedup path), draft a SUGGESTED reply
with the existing LLM client, and notify the user by push.

The system SUGGESTS — it never sends. Read-only OAuth scope reinforces that the
code has no send capability. Drafts are stored for the user to review and send
themselves.

Layout:
  providers/      EmailProvider abstraction + Gmail / Outlook / Yahoo(deferred)
  token_store.py  encrypt/decrypt OAuth tokens (reuses the credentials Fernet)
  matching.py     EXACT subject-reference -> tender matching
  attachments.py  file attachments via the existing ingest path
  draft.py        LLM-drafted suggested reply (reuses the brief LLM client)
  notify.py       push notification (reuses the push service)
  poller.py       idempotent per-inbox poll: list -> match -> file -> draft ->
                  notify -> mark-seen
"""

# Summary

<!-- What does this PR do, in 1-2 sentences? -->

## Phase / area

<!-- Which phase from PROJECT.md does this relate to? Which component? -->

## Changes

<!-- Bullet list of meaningful changes -->

-

## Checklist

- [ ] Read the relevant section of `PROJECT.md` before designing this change
- [ ] New service functions have unit tests
- [ ] If schema changed: new Alembic migration added with `downgrade()`
- [ ] `.env.example` updated if new env vars introduced
- [ ] `ruff check` passes
- [ ] `pytest` passes locally
- [ ] No secrets, credentials, or PII in code, logs, or commit messages
- [ ] If touching a portal adapter: no auto-submit path was added
- [ ] If touching the vault: re-validation engine is still mandatory (no shortcuts)
- [ ] If touching email: outbound to buyers remains draft-only

## Notes for reviewer

<!-- Anything reviewer should pay extra attention to, or open questions -->

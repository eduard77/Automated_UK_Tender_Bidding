# Web Push notifications

The dashboard offers an "enable alerts" bell in the header that subscribes the
current browser to push notifications. When a polled tender matches an
enabled filter profile, the backend dispatches a notification to every
subscriber pinned to that profile **plus** every catch-all subscriber (those
whose `filter_profile_id IS NULL`).

This document covers operator setup. The code paths live in:

- `tender-agent/src/tender_agent/services/push.py` — dispatch via `pywebpush`
- `tender-agent/src/tender_agent/api/push.py` — subscribe / unsubscribe / vapid-public-key
- `tender-agent/src/tender_agent/services/ingestion.py` — `_record_filter_matches` → `push.send_match_notifications`
- `tender-agent-dashboard/app/api/push/*` — three proxy routes
- `tender-agent-dashboard/lib/push.ts` — browser-side subscribe / unsubscribe
- `tender-agent-dashboard/components/PushBell.tsx` — the UI

## 1. Generate a VAPID keypair

VAPID (RFC 8292) keys identify your server to the push provider (FCM / Mozilla
autopush / Apple). Generate one keypair per environment (dev / staging / prod).

```bash
cd tender-agent-dashboard
npm install   # one-time, if you haven't
npm run generate-vapid
```

The script prints something like:

```
# Add to tender-agent/.env
VAPID_PUBLIC_KEY=BMNd3cLZ...
VAPID_PRIVATE_KEY=BCSJbLF...
VAPID_SUBJECT=mailto:admin@example.com
```

## 2. Configure the backend

Copy the three values into `tender-agent/.env`:

```dotenv
VAPID_PUBLIC_KEY=...
VAPID_PRIVATE_KEY=...
VAPID_SUBJECT=mailto:you@example.com   # must be a real, reachable contact
DASHBOARD_BASE_URL=http://localhost:3000
```

- `VAPID_SUBJECT` **must** be a `mailto:` URI or an HTTPS URL the push provider
  can reach if your subscriptions misbehave. A throwaway address won't get past
  some providers' filters.
- `DASHBOARD_BASE_URL` is the absolute origin used to build the `url` field of
  notification payloads. The service worker uses this to open the right tab when
  the user clicks the notification.
- **Never commit your `.env`.** Only the public key may leave the backend; the
  private key is server-only.

That's all the dashboard needs. The dashboard fetches the public key at runtime
from `GET /api/push/vapid-key`, which proxies the backend's `GET
/push/vapid-public-key`. There is no `NEXT_PUBLIC_VAPID_*` variable on the
dashboard side — by design.

If `VAPID_PUBLIC_KEY` or `VAPID_PRIVATE_KEY` is unset, the backend returns 503
from `/push/vapid-public-key`, the dashboard's proxy passes the 503 through,
and `PushBell` quietly hides itself.

## 3. Local end-to-end test

Once you have VAPID keys in `tender-agent/.env`:

```bash
# Terminal 1 — backend (assumes Postgres is running per docker-compose.yml or
# a native install with the `tender` / `tender` / `tender_agent` role+db).
cd tender-agent
alembic upgrade head
uvicorn tender_agent.main:app --host 0.0.0.0 --port 8000

# Terminal 2 — dashboard
cd tender-agent-dashboard
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 npm run dev
```

Then in a browser at `http://localhost:3000`:

1. Click **"→ enable alerts"** in the top-right of the header.
2. Grant the notification permission prompt.
3. Confirm a row appears in the DB:

   ```bash
   psql postgresql://tender:tender@localhost:5432/tender_agent \
     -c "SELECT id, endpoint, filter_profile_id, created_at FROM push_subscriptions;"
   ```

4. Trigger a dispatch. Either run a real poll (`POST /admin/poll-now`) and wait
   for an ingested tender to match a filter, or seed a filter match manually:

   ```sql
   -- Pre-req: a tender in `tenders` and a filter profile in `filter_profiles`.
   INSERT INTO filter_matches (tender_id, filter_profile_id) VALUES (1, 1);
   ```

   then in another shell:

   ```python
   from tender_agent.db import SessionLocal
   from tender_agent.models import Tender
   from tender_agent.services import push
   with SessionLocal() as db:
       t = db.get(Tender, 1)
       push.send_match_notifications(db, t, [1])
       db.commit()
   ```

5. You should see the browser notification "New tender match · <title> —
   <buyer>" appear, and clicking it should open `/tenders/{id}`.
6. Confirm `last_used_at` was updated on the subscription row.
7. Click "◉ alerts on" in the header → the row should be deleted from
   `push_subscriptions`.

## 4. Failure modes & operator notes

- **Endpoint 410 Gone**: the subscription is dead (user uninstalled the browser,
  cleared site data, etc.). Dispatch deletes the row automatically.
- **Endpoint 404**: same handling as 410.
- **`pywebpush` raises any other error**: logged as `push.send_failed` /
  `push.send_error`. The subscription is kept and will be retried on the next
  match. We never raise out of the dispatch boundary — a single broken
  subscriber can never break ingestion.
- **VAPID misconfigured**: the dispatch logs `push.skip_unconfigured` and
  no-ops. The dashboard bell hides itself.
- **No subscribers for a filter**: `push.no_subscribers` log event, no-op.

## 5. Production considerations (not yet wired)

- **HTTPS is required** for the browser's Push API. Local `http://localhost`
  is exempt, but staging/prod must be served over TLS.
- **Service worker scope** is currently `/` (set by the manifest). Don't move
  the service worker — relative scope rules are a common foot-gun.
- **Queuing**: dispatch is synchronous and per-match. For a busy backend, push
  delivery should move to a Temporal workflow (Phase 6) so the ingestion
  transaction commits without waiting for HTTPS roundtrips to the push provider.
- **Per-filter subscriptions**: the `filter_profile_id` column already supports
  pinning, but the dashboard currently only offers catch-all subscribes. A
  follow-up will add per-filter "subscribe to this profile" buttons on
  `/filters`.

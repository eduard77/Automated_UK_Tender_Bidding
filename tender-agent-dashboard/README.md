# Tender Agent — Dashboard

Next.js 15 (App Router) PWA for browsing UK public tenders, managing filter
profiles, and subscribing to Web Push notifications when filters match.

Pairs with the FastAPI backend in [`../tender-agent`](../tender-agent). The
dashboard is fully client-rendered for data (SWR) and proxies all backend
calls through its own `/api/*` routes — no CORS to worry about.

## Quickstart

```bash
cd tender-agent-dashboard
cp .env.example .env.local
npm install
npm run dev
```

Dashboard: <http://localhost:3000>

The dev server expects the backend at the URL in `NEXT_PUBLIC_API_BASE_URL`
(defaults to `http://localhost:8000`). Run `cd ../tender-agent && docker
compose up` in another terminal first.

## Pages

- `/` — tender list with source chips (FTS / CF / PCS / S2W / NI),
  debounced buyer search, status filter, matched-only toggle, pagination.
- `/filters` — filter profile CRUD with inline create + edit form, an
  enabled-toggle switch, criteria summary, "matched: N tenders" count.
- `/tenders/[id]` — tender detail: header (badge → source URL, deadline
  chip), brief (recommendation banner, mandatory + desired requirements with
  confidence chips, documents required, questions, risk flags), attached
  documents with `download_status` badges.

The header includes a `PushBell` that:

1. Fetches the VAPID public key from `/api/push/vapid-key` (proxied from the
   backend's `/push/vapid-public-key`).
2. Hides itself if the backend reports push isn't configured, or if the
   browser doesn't support push.
3. On click: requests notification permission, subscribes via
   `PushManager.subscribe`, POSTs the subscription to
   `/api/push/subscribe` (proxied to the backend).

## npm scripts

| Command | What it does |
|---|---|
| `npm run dev` | Next dev server on :3000 with hot reload. |
| `npm run build` | Production build. Used by CI. |
| `npm start` | Run the production build. |
| `npm run lint` | Next lint (eslint). |
| `npm run generate-vapid` | Print a fresh VAPID keypair to stdout. See `docs/push-setup.md`. |

## Environment

The single env var the dashboard reads is documented in
[`.env.example`](.env.example):

- `NEXT_PUBLIC_API_BASE_URL` — backend URL. Local: `http://localhost:8000`.
  Docker compose between containers: `http://api:8000`.

The dashboard does **not** read the VAPID public key from env vars — it
fetches it from the backend at runtime. This keeps configuration in one place
(`tender-agent/.env`) and avoids baking a key into the build output. See
[`docs/push-setup.md`](../docs/push-setup.md) for the full setup.

## Architecture notes

- **App Router only.** No Pages Router.
- **Server components by default**; `"use client"` only where state is needed
  (TenderList, FiltersManager, TenderDetail, PushBell).
- **Tailwind** with custom tokens in `tailwind.config.mjs` (ink / bone /
  oxblood / moss / sage / rust palette; Fraunces / Inter / JetBrains Mono).
- **API client** in `lib/api.ts` is the only path that calls `fetch()`. Don't
  call `fetch()` directly from components.
- **Web Push** browser plumbing in `lib/push.ts`. Backend dispatch in
  `tender-agent/src/tender_agent/services/push.py`.
- **Strict TypeScript.** No `any` — use `unknown` and narrow.

## Layout

```
app/
├── layout.tsx                Root layout + header (always renders PushBell)
├── page.tsx                  Tender list (server shell)
├── filters/page.tsx          Filters page (server shell)
├── tenders/[id]/page.tsx     Tender detail (server shell)
└── api/push/
    ├── subscribe/route.ts    → backend POST /push/subscriptions
    ├── unsubscribe/route.ts  → backend DELETE /push/subscriptions
    └── vapid-key/route.ts    → backend GET /push/vapid-public-key (cached 5 min)
components/
├── TenderList.tsx            SWR list, source chips, pagination, debounced search
├── FiltersManager.tsx        CRUD + optimistic toggle/delete
├── TenderDetail.tsx          Header / brief / requirements / attached docs
├── TenderCard.tsx            Used by TenderList and the detail page
└── PushBell.tsx              Subscribe/unsubscribe state machine
lib/
├── api.ts                    Typed fetch helpers + types mirroring backend schemas
└── push.ts                   Browser-side push helpers (subscribe / unsubscribe / vapid)
public/
├── manifest.json             PWA manifest
├── sw.js                     Service worker (push + notification click handler)
└── icons/                    Solid #0E1116 placeholders for now
scripts/
└── generate-vapid.mjs        VAPID keypair generator
```

## Tests / verification

The dashboard doesn't have unit tests yet; rely on `npx tsc --noEmit` +
`npm run build` + a manual smoke pass against the live backend. Both checks
are gated by CI.

A typical end-to-end smoke loop:

```bash
# Terminal 1
cd tender-agent && docker compose up

# Terminal 2
cd tender-agent-dashboard && npm run dev

# Browser
open http://localhost:3000
# Create a filter → POST /admin/poll-now from another terminal → wait → see
# tenders → click a tender → confirm the detail page → click "enable alerts"
# → confirm push_subscriptions row appears in the DB.
```

# Tender Agent — Browser Bridge

This is a small helper program that runs **on your own PC** (not in Docker). Its
job is to open a **real Chrome window that you log into yourself**, so the system
can fetch tender documents from portals like Delta eSourcing — without ever
seeing or storing your password.

## Why it exists

The main Tender Agent backend runs inside Docker (a Linux container). A program
inside Docker can't pop up a window on your Windows desktop. So the browser runs
out here, natively, and the backend tells it what to do over a small local
connection secured by a shared secret token.

**Your password is never stored.** You type it into the Chrome window the bridge
opens, exactly like logging into any website. The bridge only keeps the
resulting login *cookies* on disk so you don't have to log in again every time.

## One-time setup

1. Make sure Python 3.12 is installed.
2. In this folder, create a file called `.env` containing a shared secret — the
   **same** value you put in the backend's `.env` as `TENDER_AGENT_BRIDGE_TOKEN`:

   ```
   TENDER_AGENT_BRIDGE_TOKEN=pick-a-long-random-string
   ```

## Starting it

Double-click **`start-bridge.ps1`** (or run it in PowerShell). The first run
takes a minute while it sets itself up and downloads Chrome. When you see:

```
Bridge running on http://localhost:8765 — leave this window open.
```

…it's ready. **Leave that window open** while you use the document-fetch feature.

In the dashboard header you'll see a **Bridge ●** indicator — green when the
bridge is running, grey when it isn't.

## Using it

1. Start the bridge (above).
2. In the dashboard, open a tender on a login portal and click **Fetch
   documents**.
3. A Chrome window appears at the portal's login page. **Log in there**, as
   normal.
4. The fetch continues automatically once you're logged in. Next time on the
   same portal, you usually won't need to log in again (the session is
   remembered) until it expires.

If a portal requires you to **"Express Interest"** before releasing documents,
the dashboard will pause and ask you to confirm first — because that tells the
buyer you intend to bid. Nothing is clicked on your behalf without that confirm.

## Delta eSourcing: one login at a time

Delta only allows **one active login per account at a time** ("Concurrent Logins
Are Not Enabled"). While the bridge is using your Delta session, you can't also
be logged into Delta in your own browser, and vice-versa.

To avoid locking you out, the bridge now **logs out of Delta and releases the
session automatically** when a fetch finishes (whether it succeeded or failed).
So once a fetch completes, you're free to use Delta yourself.

Two things to know:

- If you start a fetch while you're **already logged into Delta** elsewhere, the
  fetch fails fast with a clear message ("Delta session conflict…"). End your
  other Delta session, then retry.
- If you ever still see "the login details provided are currently in use", use
  Delta's **"End Session"** email link to force-release the stuck session, then
  retry.

## Stopping it

Press **Ctrl+C** in the bridge window, or just close it. Your saved logins stay
on disk for next time.

## Where things live

- Saved sessions (cookies): `%USERPROFILE%\.tender-agent\bridge-sessions\`
- Downloaded files: `%USERPROFILE%\.tender-agent\bridge-downloads\`

The downloads folder is shared with the backend via a Docker volume so fetched
documents flow back into the app (see the root `docker-compose.yml`).

## Configuration (env vars)

| Variable | Default | Purpose |
| --- | --- | --- |
| `TENDER_AGENT_BRIDGE_TOKEN` | (none) | Shared secret; must match the backend. Required. |
| `TENDER_AGENT_BRIDGE_PORT` | `8765` | Port the bridge listens on. |
| `TENDER_AGENT_BRIDGE_STATE_DIR` | `%USERPROFILE%\.tender-agent\bridge-sessions` | Where login cookies persist. |
| `TENDER_AGENT_BRIDGE_DOWNLOAD_DIR` | `%USERPROFILE%\.tender-agent\bridge-downloads` | Where downloads land (shared with backend). |
| `TENDER_AGENT_BRIDGE_HEADLESS` | (unset → visible) | Set to `1` to run with no visible window (used only by the automated test). |

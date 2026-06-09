# Email integration — one-time provider setup

The email feature connects a user's inbox over **OAuth (read-only)**, watches
for tender emails by the **exact tender reference in the subject**, files the
email + attachments against the matching tender, drafts a **suggested** reply
(never sent), and pushes a phone notification.

Before anyone can connect an inbox, **you (the operator) must register an OAuth
app with each provider, using your own access** to that provider's console.
Claude Code cannot do these registrations — they require your accounts. This
document is the checklist.

Each provider gives you a **client id** + **client secret**. Put them in the
backend environment (locally in `.env`, in production in your secrets store)
using the exact variable names below. Until they are set, the connect flow
returns a clear `… is not configured yet` message instead of a cryptic error.

**One redirect URI, shared by all providers:**

```
EMAIL_OAUTH_REDIRECT_URI = https://<your-api-host>/email/oauth/callback
```

For local dev that is typically `http://localhost:8000/email/oauth/callback`.
Register this **exact** string with every provider — a single character
mismatch makes the provider reject the callback.

---

## 1. Google / Gmail  (fully implemented)

API: Gmail API. Scope: `https://www.googleapis.com/auth/gmail.readonly`
(read-only — no send capability is requested or granted).

1. Go to the **Google Cloud Console** → create or select a project.
2. **APIs & Services → Library →** enable the **Gmail API**.
3. **APIs & Services → OAuth consent screen:**
   - User type **External** (or **Internal** if you use Google Workspace and
     only your org connects).
   - Fill app name, support email, developer contact.
   - **Scopes →** add `…/auth/gmail.readonly`.
   - While the app is in **Testing**, add each mailbox you'll connect under
     **Test users** (publish the app to remove that restriction).
4. **APIs & Services → Credentials → Create credentials → OAuth client ID:**
   - Application type **Web application**.
   - **Authorized redirect URIs →** add your `EMAIL_OAUTH_REDIRECT_URI` exactly.
5. Copy the generated **Client ID** and **Client secret** into:

   ```
   GMAIL_CLIENT_ID      = <client id>
   GMAIL_CLIENT_SECRET  = <client secret>
   ```

The app requests `access_type=offline` + `prompt=consent`, so Google returns a
refresh token; the backend refreshes access tokens automatically.

---

## 2. Microsoft / Outlook  (fully implemented)

API: Microsoft Graph mail. Scopes: `Mail.Read offline_access User.Read`
(read-only — `Mail.Send` is deliberately **not** requested).

1. Go to the **Azure Portal → Microsoft Entra ID → App registrations → New
   registration.**
   - **Supported account types:** "Accounts in any organizational directory and
     personal Microsoft accounts" if you want both work and personal inboxes
     (this matches `MS_TENANT=common`).
   - **Redirect URI:** platform **Web**, value = your `EMAIL_OAUTH_REDIRECT_URI`
     exactly.
2. **API permissions → Add a permission → Microsoft Graph → Delegated
   permissions →** add `Mail.Read`, `offline_access`, `User.Read`. (Delegated,
   not Application.) Grant admin consent if your tenant requires it.
3. **Certificates & secrets → New client secret →** copy the secret **Value**
   (not the secret ID) immediately — it's shown once.
4. From **Overview**, copy the **Application (client) ID**. Put both into:

   ```
   MS_CLIENT_ID      = <application (client) id>
   MS_CLIENT_SECRET  = <client secret value>
   MS_TENANT         = common      # or your specific tenant id
   ```

---

## 3. Yahoo  (DEFERRED — not yet available)

Yahoo does **not** offer a clean read-only mail REST API equivalent to Gmail API
or Microsoft Graph. Inbox access is **OAuth2 + IMAP** (the `XOAUTH2`
mechanism). Rather than block Gmail and Outlook on building an IMAP read loop,
Yahoo is **deferred**: the provider exists behind the same interface but reports
`not yet configured`, and the connect button is disabled for it.

When implemented, the one-time setup will be:

1. **Yahoo Developer Network → Create an App.**
2. Request the **Mail – Read** permission (OAuth2).
3. Set the redirect URI to your `EMAIL_OAUTH_REDIRECT_URI`.
4. Copy the **Client ID (Consumer Key)** + **Client Secret (Consumer Secret)**
   into `YAHOO_CLIENT_ID` / `YAHOO_CLIENT_SECRET`.

The remaining work is an IMAP-over-OAuth2 implementation of the `EmailProvider`
interface in `services/email/providers/yahoo.py` (list/fetch via `imaplib` with
an `XOAUTH2` SASL string). The interface slot, config keys, and "not yet
configured" behaviour are already in place and tested.

---

## After the secrets are set

1. Set `EMAIL_POLL_ENABLED=true` to start the scheduled per-inbox poll
   (interval `EMAIL_POLL_INTERVAL_MINUTES`, default 5).
2. Restart the backend so the scheduler picks up the new job.
3. **First real test:** sign in to the dashboard, connect your own Gmail (or
   Outlook), then send yourself an email whose **subject contains a tender
   reference you already hold** (a `source_ref` or `procurement_ref`). Within a
   poll interval — or immediately via `POST /email/connections/{id}/poll` — the
   email and its attachments are filed against that tender, a suggested reply
   draft appears, and you receive a push notification.

The system only ever **suggests**. It has no send capability (read-only scope),
never follows links in the body (attachments only), and matches on the **exact**
reference only.

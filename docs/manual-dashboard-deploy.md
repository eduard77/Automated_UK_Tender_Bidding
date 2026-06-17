# Manual dashboard deploy (free, no GitHub Actions, no Windows zip)

How to deploy `tender-agent-dashboard` to the Azure App Service
**genera-tenders-dashboard** by hand, for free, while GitHub Actions minutes
are exhausted — without hitting the Windows-zip backslash corruption.

## Why the previous manual attempt failed

A Windows-made zip stores paths with **backslashes**. Kudu unpacks on Linux,
where `\` is a normal filename character, so every entry became one long
mangled name → `parallel_rsync` reported `0 regular files transferred`
(`failed to stat ... Invalid argument (22)`). **Lesson: never zip on Windows
for a Linux App Service.**

## How it deployed before (GitHub Actions)

`.github/workflows/deploy-dashboard.yml` (Node 22 runner), all inside the
`tender-agent-dashboard` subfolder:

1. `npm ci`
2. `npm run build` — with **`NEXT_PUBLIC_API_BASE_URL` set as a build-time env**
   so Next.js bakes it into the client bundle.
3. `npm prune --omit=dev`, then delete `.next/cache`.
4. Upload the whole self-contained subfolder (pruned `node_modules` + `.next`)
   via the publish profile; App Service just runs `npm start` (= `next start`).

So Actions **built on the runner and shipped the build**. The manual flow
below instead lets **Azure build on its own Linux side** (Oryx), so we never
ship — or zip — `node_modules`/`.next` at all.

## The chosen mechanism: `az webapp up` from inside the subfolder

`az webapp up`, run from `tender-agent-dashboard`, is the best fit because:

- The **Azure CLI builds the zip itself with forward slashes** on a Python
  zip writer — the Windows backslash bug is structurally impossible.
- Run from the subfolder, it uploads **only that folder**, so Oryx sees
  `package.json` at the zip root and builds the dashboard — no monorepo
  `PROJECT` / `.deployment` indirection needed.
- With `SCM_DO_BUILD_DURING_DEPLOYMENT=true`, **Oryx runs `npm install` +
  `npm run build` on Azure's Linux side**, mirroring the old Actions build
  but on the server. `node_modules`/`.next` never leave the workstation.

(Kudu local-git was the alternative, but it pushes the whole monorepo and
needs a `PROJECT`/`.deployment` pointer to the subfolder — more moving parts
for no benefit here.)

## What this repo carries to support it

- `tender-agent-dashboard/package.json` → `"engines": { "node": "22.x" }`
  so Oryx picks the same Node major the Actions runner used.
- `tender-agent-dashboard/.gitignore` so `az webapp up` excludes
  `node_modules`, `.next`, `*.tsbuildinfo`, `deploy.zip`, and `.env*` from the
  uploaded zip (it reads the `.gitignore` of the folder it runs in).
- `start` script is already `next start`, which honours the `PORT` env var
  Azure provides — no change needed.

---

## Commands (Windows PowerShell)

### One-time setup

```powershell
# Sign in (opens a browser) and select the subscription.
az login
az account set --subscription c7b1560d-735e-4d60-817a-b8d9b10b3498

# Build on Azure's Linux side during deploy, AND bake the backend URL into the
# client bundle at build time (Next.js inlines NEXT_PUBLIC_* during the build).
az webapp config appsettings set --name genera-tenders-dashboard --resource-group GeneraSystems20260318181723ResourceGroup --settings SCM_DO_BUILD_DURING_DEPLOYMENT=true NEXT_PUBLIC_API_BASE_URL=https://generatender-gqbgaye9fmdfc4c6.ukwest-01.azurewebsites.net

# Make the start command explicit (belt-and-suspenders; Oryx defaults to this).
az webapp config set --name genera-tenders-dashboard --resource-group GeneraSystems20260318181723ResourceGroup --startup-file "npm run start"

# Delete the corrupt 93 MB Windows zip from the failed attempt, if still there.
Remove-Item C:\Code\PublicTender\tender-agent-dashboard\deploy.zip -ErrorAction SilentlyContinue
```

### Every deploy

```powershell
cd C:\Code\PublicTender\tender-agent-dashboard
az webapp up --name genera-tenders-dashboard --resource-group GeneraSystems20260318181723ResourceGroup --runtime "NODE:22-lts"
```

That single command zips the subfolder (forward slashes), uploads it, and Oryx
runs `npm install` + `npm run build` + starts `next start`. First run takes a
few minutes (full install); watch the streamed build log it prints.

### Verify

```powershell
az webapp log tail --name genera-tenders-dashboard --resource-group GeneraSystems20260318181723ResourceGroup
```

Then open `https://genera-tenders-dashboard.azurewebsites.net` (or the app's
configured hostname) and confirm the dashboard loads and talks to the backend.

---

## Risks / differences vs the old Actions build

- **Build-time env var is the #1 risk.** Actions set
  `NEXT_PUBLIC_API_BASE_URL` for the build; Oryx only sees it if it's an **App
  Setting before the build runs** (set in one-time setup above). If it's
  missing, the client bundle calls the wrong API origin. Changing it later
  requires a **redeploy**, not just a restart — it's baked at build time.
- **Oryx vs runner parity.** Oryx runs `npm install` (not `npm ci`) and may
  resolve slightly newer patch versions within the `^` ranges than the
  committed `package-lock.json` pinned. Functionally equivalent, but not
  byte-identical to the Actions build.
- **First deploy is slow** (cold `npm install` on the server) and the app may
  502 for ~30–60 s while Oryx builds and `next start` boots — wait and retry.
- **Startup command.** If the site shows the default Azure splash, set the
  startup command (one-time step above) and restart:
  `az webapp restart --name genera-tenders-dashboard --resource-group GeneraSystems20260318181723ResourceGroup`.
- This flow **does not touch** the backend (`tender-agent`), `browser-bridge`,
  classification, or discovery — dashboard only.

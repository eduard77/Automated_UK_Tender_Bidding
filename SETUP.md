# Setup Instructions

Follow these steps in order. Estimated time: 30-45 minutes.

## What's in this zip

```
repo-infra/
├── .gitignore                          ← Prevents committing secrets/junk
├── README.md                           ← Top-level repo README
├── LICENSE                             ← Proprietary licence
└── .github/
    ├── CODEOWNERS                      ← Automatic review assignment
    ├── dependabot.yml                  ← Weekly dependency PRs
    ├── pull_request_template.md        ← Enforces project conventions on PRs
    ├── workflows/
    │   └── ci.yml                      ← Runs lint + tests on every PR
    └── ISSUE_TEMPLATE/
        ├── bug_report.yml
        └── task.yml
```

## Step 1 — Combine with the handoff zip

You should have two zips in your Downloads (or wherever):
- `tender-agent-handoff.zip` — the actual code
- `repo-infra.zip` — these infra files (this one)

Create a working folder and put both projects together:

```bash
# Pick a sensible location
mkdir -p ~/projects/Automated_UK_Tender_Bidding
cd ~/projects/Automated_UK_Tender_Bidding

# Unzip the code (it contains tender-agent/ and tender-agent-dashboard/)
unzip ~/Downloads/tender-agent-handoff.zip

# Unzip the infra files on top — they're complementary, no conflicts
unzip ~/Downloads/repo-infra.zip
# If the infra zip unpacks into a `repo-infra/` folder, move its contents up:
# (run only if you see a repo-infra/ folder inside your project dir)
mv repo-infra/.* repo-infra/* . 2>/dev/null
rmdir repo-infra 2>/dev/null
```

After this, `ls -la` should show: `.github/`, `.gitignore`, `LICENSE`, `README.md`,
`tender-agent/`, `tender-agent-dashboard/`.

## Step 2 — Initialise git and push

```bash
cd ~/projects/Automated_UK_Tender_Bidding
git init
git branch -M main
git remote add origin https://github.com/eduard77/Automated_UK_Tender_Bidding.git

# Sanity check: confirm .env is NOT in this list
git status

git add .
git commit -m "Initial commit: Phase 1 complete, Phase 2 partial, handoff docs"
git push -u origin main
```

If `git push` asks for credentials and you don't have a Personal Access Token set up:
- GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
- Generate new token (classic), scope `repo`, expiry 90 days
- Use that token as the password when git prompts

## Step 3 — Add the Anthropic API key as a GitHub secret

This lets CI run tests that touch the Claude API.

1. Open <https://github.com/eduard77/Automated_UK_Tender_Bidding/settings/secrets/actions>
2. Click **New repository secret**
3. Name: `ANTHROPIC_API_KEY`
4. Value: paste your Anthropic key (the one starting `sk-ant-api03-...`)
5. Click **Add secret**

## Step 4 — Protect the main branch

Prevents broken code from being pushed directly.

1. Open <https://github.com/eduard77/Automated_UK_Tender_Bidding/settings/branches>
2. Click **Add branch protection rule** (or "Add rule")
3. Branch name pattern: `main`
4. Tick:
   - **Require a pull request before merging**
     - Require approvals: 0 (you're solo for now — change to 1 if you add collaborators)
   - **Require status checks to pass before merging**
     - Search and add: `Backend (Python)` and `Frontend (Next.js)`
     - Tick "Require branches to be up to date before merging"
   - **Do not allow bypassing the above settings** — leave **unchecked** for now (you'll
     need to push fixes directly while bootstrapping)
5. Click **Create**

> **Note**: the status checks only appear in the dropdown after CI has run at least
> once. If you don't see them, do step 5 first, wait for CI to complete, then come
> back to step 4.

## Step 5 — Verify CI runs

After your first push:

1. Open <https://github.com/eduard77/Automated_UK_Tender_Bidding/actions>
2. You should see a workflow run in progress called "CI"
3. Wait 2-5 minutes for both `Backend (Python)` and `Frontend (Next.js)` jobs to
   complete
4. **Both should show green ✓**

If either fails, click into the failed job to see the error. Common first-time
failures:
- Backend: a real-world adapter URL doesn't match documented shape (not blocking — tests
  use fixtures, not live APIs)
- Frontend: missing `package-lock.json` (CI will install fresh; should still pass)

If anything goes wrong, paste the failure output back to me and I'll debug.

## Step 6 — Enable Dependabot

1. Open <https://github.com/eduard77/Automated_UK_Tender_Bidding/settings/security_analysis>
2. Under "Dependabot", enable:
   - **Dependabot alerts**
   - **Dependabot security updates**
   - **Dependabot version updates** (this picks up the `dependabot.yml` we just added)

## Step 7 — Run the project locally to verify it works

```bash
cd ~/projects/Automated_UK_Tender_Bidding/tender-agent
cp .env.example .env
```

Open `.env` in your editor and paste your Anthropic key:

```
ANTHROPIC_API_KEY=sk-ant-api03-...your-key-here...
```

Save. Then start the stack:

```bash
docker compose up --build
```

Wait for "Application startup complete." Then in a second terminal:

```bash
# Health check
curl http://localhost:8000/health
# Should return: {"status":"ok","version":"0.1.0"}

# Create a filter profile (example: cleaning services)
curl -X POST http://localhost:8000/filters \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Cleaning services UK",
    "cpv_prefixes": ["909"],
    "min_days_to_deadline": 7
  }'

# Trigger an immediate poll
curl -X POST http://localhost:8000/admin/poll-now

# Wait 30 seconds, then list tenders
curl "http://localhost:8000/tenders?limit=5" | jq
```

If tenders are returned, **the discovery service is working against live UK APIs.**
If you get an empty list or errors, the adapter base URLs may need adjusting — let me
know and I'll help.

## What's next

After all 7 steps pass:

1. Reply to me with:
   - "Setup done, CI green, local works" (or what failed)
   - Which portal you want for Phase 4 (ProContract, In-Tend, Jaggaer, Delta, Atamis,
     Multiquote, or other)
   - Which domain you'll use for the dashboard
2. I'll send the Phase 2 completion: dashboard pages + AWS Terraform.
3. After that, we move to Phase 3 (vault) and your first portal adapter.

If anything goes wrong at any step, copy the error message and send it to me.

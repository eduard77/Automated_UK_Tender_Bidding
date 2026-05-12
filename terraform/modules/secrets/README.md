# secrets

Secrets Manager containers for the Tender Agent stack. The module creates the
secrets but **does not set their values** — operators populate them out of
band so real keys never appear in Terraform state, the plan, or version
control.

## What it creates

| Secret name | Consumer(s) | Purpose tag | What to put in it |
|---|---|---|---|
| `tender-agent-<env>/anthropic-api-key` | api, worker | `anthropic-api-key` | Plain string — the `sk-ant-…` key |
| `tender-agent-<env>/vapid-private-key` | api | `vapid-private-key` | Plain string — the VAPID private key from `npm run generate-vapid` |
| `tender-agent-<env>/vapid-public-key`  | api | `vapid-public-key`  | Plain string — the matching public key |
| `tender-agent-<env>/vapid-subject`     | api | `vapid-subject`     | Plain string — `mailto:ops@example.com` (or https URL) |
| `tender-agent-<env>/dashboard-session-secret` | api (when wired) | `dashboard-session-secret` | Random 32-byte base64 — leave unset until session-signing lands |
| `tender-agent-<env>/portal-credentials-placeholder` | none | `portal-credentials-placeholder` | **DO NOT POPULATE**. Documents the Phase 4 shape only |

Phase 4 portal adapters add one secret each at
`tender-agent-<env>/portal/<portal-key>` with value
`{"username": "...", "password": "...", "totp_seed": "..." (optional)}`.

Every secret is tagged with `project=tender-agent`, `environment=<env>`,
`managed-by=terraform`, plus `purpose=...` and `consumers=api|worker|both|none`
so operators can grep the AWS console by consumer.

## Populating values

After `terraform apply`, per environment:

```bash
ENV=staging   # or prod

aws secretsmanager put-secret-value \
  --secret-id tender-agent-$ENV/anthropic-api-key \
  --secret-string "$ANTHROPIC_API_KEY"

# VAPID keypair — generate once with `cd tender-agent-dashboard && npm run generate-vapid`
aws secretsmanager put-secret-value \
  --secret-id tender-agent-$ENV/vapid-private-key \
  --secret-string "$VAPID_PRIVATE_KEY"

aws secretsmanager put-secret-value \
  --secret-id tender-agent-$ENV/vapid-public-key \
  --secret-string "$VAPID_PUBLIC_KEY"

aws secretsmanager put-secret-value \
  --secret-id tender-agent-$ENV/vapid-subject \
  --secret-string "mailto:ops@yourdomain.example"

# Optional — only when session-signing lands. 32 random bytes, base64.
aws secretsmanager put-secret-value \
  --secret-id tender-agent-$ENV/dashboard-session-secret \
  --secret-string "$(openssl rand -base64 32)"
```

## Why values aren't in Terraform

- Plain values would land in `.tfstate` (encrypted at rest, but readable to
  anyone with state-bucket access).
- Plan output during `terraform apply` would print the values.
- A leaked CI artefact, log, or screen-share would expose every secret in
  one place.

Keeping the values out of Terraform keeps the blast radius of every shared
artefact small, and means secret rotation never needs a `terraform apply`.

## Why each VAPID component is its own secret

VAPID rotation in practice means rotating the private key while keeping the
public key the same for a transition window so existing subscriptions keep
working. Keeping public, private, and subject as separate secrets lets
operators rotate the private key without re-staging the other two. A single
JSON secret would force all three to roll together.

## Inputs

| Variable | Required | Default | Description |
|---|---|---|---|
| `environment` | yes | — | e.g. `staging`, `prod`. Goes into secret names + the `environment` tag. |
| `kms_key_id`  | no  | `null` | KMS key for envelope encryption. Null falls back to `aws/secretsmanager` (AWS-managed). Use a customer-managed key in prod if data-classification requires it. |
| `recovery_window_in_days` | no | `7` | Days a deleted secret stays recoverable. `0` for staging (immediate re-create); `7-30` for prod. |
| `tags` | no | `{}` | Extra tags merged on top of the module's defaults. |

## Outputs

- Per-secret ARNs: `anthropic_api_key_arn`, `vapid_private_key_arn`,
  `vapid_public_key_arn`, `vapid_subject_arn`, `dashboard_session_secret_arn`,
  `portal_credentials_placeholder_arn`.
- `all_arns` — list of all secret ARNs; convenient for IAM `Resource` blocks.
- `arns_by_env_var` — map keyed by the canonical env-var name an ECS task
  would mount the secret as. Iterate this when building task definitions
  so adding a new secret here automatically threads through to the running
  services.

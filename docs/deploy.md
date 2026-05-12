# Deploy runbook

End-to-end deploy of the Tender Agent backend on AWS, against the Terraform
package in [`terraform/`](../terraform/). **The deploy itself has not been
performed yet** — this runbook is the package; the operator runs it once the
gating decisions land:

1. **AWS account access** (a sandbox sub-account, ideally; permissions in §1
   below).
2. **Dashboard hosting decision** — the dashboard ships separately as part of
   the genera-system.com integration. See
   [`../terraform/modules/cloudfront/README.md`](../terraform/modules/cloudfront/README.md).
3. **Budget approval** — see [§7 Cost expectations](#7-cost-expectations).

## 1. Prerequisites

### AWS account & IAM

- AWS account in the **eu-west-2 (London)** region (UK data residency).
- An IAM user (or SSO role) the operator runs Terraform with — call it
  `tender-agent-admin`. It needs:
  - Full read/write on: VPC, EC2 (for SGs/EIPs/NATs), ECS, ECR, RDS, S3,
    IAM, Secrets Manager, ACM, Route53, CloudWatch, ALB.
  - `iam:PassRole` to itself for ECS task/execution roles.
- For the CI `Build & Push API image` workflow: an IAM role usable via
  GitHub Actions OIDC. The trust policy condition limits to this repo:
  ```
  "token.actions.githubusercontent.com:sub": "repo:eduard77/Automated_UK_Tender_Bidding:ref:refs/heads/main"
  ```
  Grant only ECR push to the repository created by bootstrap.

### Tooling on the operator's workstation

- Terraform 1.6+ (matches the CI lane in `.github/workflows/terraform-validate.yml`).
- AWS CLI v2.
- Docker (only if you want to build images locally — usually CI does this).
- `psql` 16+ for the one-time `CREATE EXTENSION vector;`.

### DNS / domain

- A domain you control to CNAME the ALB from. The dashboard hostname is
  separate (genera-system.com integration; see PR comment for status).
- An ACM certificate in **eu-west-2** for the operator domain — request via
  `aws acm request-certificate --domain-name api.<your-domain>` and validate
  via DNS. The cert ARN goes into `alb_certificate_arn` in prod.

### Secrets to create (post-apply)

| Secret | Value | Notes |
|---|---|---|
| `<env>/anthropic-api-key` | `sk-ant-api03-…` | Plain string. |
| `<env>/vapid` | `{"public_key": "...", "private_key": "...", "subject": "mailto:..."}` | JSON. Generate with `cd tender-agent-dashboard && npm run generate-vapid`. |

The RDS master password is **created automatically** by AWS and stored in
the Secrets Manager secret referenced by `module.rds.master_user_secret_arn`
— operator doesn't set it.

## 2. One-time bootstrap

Bootstraps:
- S3 bucket for remote Terraform state.
- DynamoDB table for state locking.
- ECR repository for the api image.

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

Capture the outputs — `tfstate_bucket`, `tfstate_lock_table`, `ecr_repository_url`.
If you keep the defaults, the env backend files (`envs/{staging,prod}/backend.tf`)
already point at them.

**Back up** `terraform/bootstrap/terraform.tfstate` somewhere outside the
repo. Losing it requires importing the bootstrap resources by hand.

### Push the first container image

Either let the CI workflow do it (push to `main` triggers
`build-and-push-image.yml` once `ECR_REPOSITORY`, `AWS_REGION`, and
`AWS_ROLE_TO_ASSUME` are set as repo variables), or build locally:

```bash
cd tender-agent
docker build -t "${ECR_URL}:bootstrap" .
aws ecr get-login-password --region eu-west-2 \
  | docker login --username AWS --password-stdin "${ECR_URL%/*}"
docker push "${ECR_URL}:bootstrap"
```

Note the tag — you'll pass it as `api_image` to the env apply below.

## 3. First env deploy — staging walkthrough

```bash
cd terraform/envs/staging
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars: set api_image to the ECR URL:tag from step 2.
terraform init
terraform plan -out staging.tfplan
terraform apply staging.tfplan
```

Expected wall-clock: **15–25 minutes** (RDS is the long pole).

### Post-apply: load secrets + extension + migrations

#### Load Anthropic key

```bash
aws secretsmanager put-secret-value \
  --secret-id tender-agent-staging/anthropic-api-key \
  --secret-string "$ANTHROPIC_API_KEY"
```

#### Load VAPID keypair

```bash
cd tender-agent-dashboard
npm run generate-vapid > /tmp/vapid.txt

# Paste the JSON below — public_key, private_key, subject lines from vapid.txt.
aws secretsmanager put-secret-value \
  --secret-id tender-agent-staging/vapid \
  --secret-string '{
    "public_key": "...",
    "private_key": "...",
    "subject": "mailto:you@example.com"
  }'
```

#### Enable pgvector + run migrations

RDS Postgres 16 ships with `pgvector` installable as an extension. The first
time the DB is reached:

```bash
RDS_ENDPOINT=$(terraform output -raw rds_endpoint)
RDS_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw rds_master_secret_arn)" \
  --query SecretString --output text | jq -r .password)

PGPASSWORD="$RDS_PASSWORD" psql \
  "host=$RDS_ENDPOINT port=5432 user=tender dbname=tender_agent sslmode=require" \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"

DATABASE_URL="postgresql+psycopg://tender:${RDS_PASSWORD}@${RDS_ENDPOINT}:5432/tender_agent?sslmode=require" \
  alembic upgrade head
```

The `alembic upgrade head` is currently safest run from a workstation with
private-subnet reachability (VPN, bastion, or SSM session-manager into a
temporary EC2). Once we automate it, the worker container will run it on
startup.

#### Smoke the api

```bash
ALB=$(terraform output -raw alb_dns_name)
curl "http://${ALB}/health"
# {"status":"ok","version":"0.1.0"}
```

If you get a 502 from the ALB, the api task hasn't passed the health check
yet — wait 60s and retry. Beyond that, check
`/aws/ecs/tender-agent-staging-api` in CloudWatch.

#### Verify a real poll

```bash
curl -X POST "http://${ALB}/admin/poll-now"
sleep 60
curl "http://${ALB}/tenders?limit=5" | jq
```

If the response is empty after 5 minutes, check the worker logs:
`/aws/ecs/tender-agent-staging-worker`.

## 4. Subsequent deploys (image-only changes)

1. Merge the change to `main`. The `Build & Push API image` workflow pushes
   a new image tagged with the commit SHA.
2. Update `terraform/envs/staging/terraform.tfvars` (or prod):
   ```
   api_image = "<ecr_url>:<sha>"
   ```
3. From `terraform/envs/<env>/`:
   ```bash
   terraform plan -out env.tfplan
   terraform apply env.tfplan
   ```
   Terraform updates the task definition; ECS rolls the new tasks in with a
   circuit-breaker-gated deployment.
4. Watch the ECS service events in the AWS console (or
   `aws ecs describe-services`) until both tasks are `RUNNING` against the
   new task definition.

Migrations: if the deploy includes an Alembic revision, run `alembic upgrade head`
**before** the new image goes live (the migration framework is forwards-
compatible; the new image runs against the new schema, but the OLD image may
not run against the new schema — old → new migration order matters).

## 5. Rollback

The fastest rollback is re-applying the previous image tag:

```bash
cd terraform/envs/<env>
terraform apply -var "api_image=<previous_ecr_url>:<previous_sha>"
```

ECS rolls the previous task definition in. The circuit breaker stops the
rollout automatically if the new tasks fail their health checks twice.

If a migration broke things: Alembic supports `alembic downgrade -1`. **Test
the downgrade on staging first** — many migrations are not safely reversible
once data has been written against the new schema. PROJECT.md's data model
guidance says supersede in place rather than destroy, which means downgrades
should be safe by construction, but verify per-migration.

## 6. Viewing logs

CloudWatch log groups created by the Terraform:

- `/aws/ecs/tender-agent-<env>-api` — FastAPI logs (structlog JSON).
- `/aws/ecs/tender-agent-<env>-worker` — APScheduler + ingestion + push.
- `/aws/rds/instance/tender-agent-<env>-db/postgresql` — Postgres logs.

Each container also writes to its own log stream prefixed by the service
name (e.g. `api/api/<task-id>`).

CLI:

```bash
aws logs tail /aws/ecs/tender-agent-staging-api --follow --since 5m
aws logs filter-log-events --log-group-name /aws/ecs/tender-agent-staging-worker \
  --filter-pattern '{ $.event = "ingest.failed" }'
```

The structured log events documented in
[`observability.md`](observability.md) are all filterable with the syntax
above.

## 7. Cost expectations

Estimates for **eu-west-2**, mid-2026 pricing, **24/7 running**. These are
load-light steady-state; CloudFront + ECR egress fees not included.

### Staging — ~£90 / month

| Resource | Spec | Monthly £ |
|---|---|---|
| RDS Postgres | `db.t4g.medium`, 50 GB gp3, single-AZ | ~ £45 |
| ECS Fargate | api + worker, 0.5 vCPU + 1 GiB each | ~ £22 |
| NAT Gateway | single, shared by both AZs | ~ £25 |
| ALB | minimal traffic | ~ £15 |
| Everything else | S3, Secrets Manager, CloudWatch | < £5 |

Bring staging down with `terraform destroy` when not in use to save ~£3/day.

### Prod — ~£260 / month (light traffic)

| Resource | Spec | Monthly £ |
|---|---|---|
| RDS Postgres | `db.m6g.large`, 100 GB gp3, multi-AZ | ~ £180 |
| ECS Fargate | api × 2 + worker × 1, 1 vCPU + 2 GiB each | ~ £50 |
| NAT Gateway | one per AZ (2 total) | ~ £50 |
| ALB | with HTTPS | ~ £18 |
| Anthropic API | ~30 matched tenders/day × ~£0.05/extract | ~ £45 |
| Everything else | S3, Secrets Manager, CloudWatch | ~ £10 |

Cost grows mostly with Anthropic usage (proportional to number of matched
tenders). Bound the cost ceiling by capping `desired_count` and the Anthropic
spend with a usage limit on the API key.

## 8. Destroying

Staging only.

```bash
cd terraform/envs/staging

# Empty S3 buckets first — terraform refuses to delete non-empty buckets.
DOCS_BUCKET=$(terraform output -raw documents_bucket_name)
DEBUG_BUCKET=$(terraform output -raw debug_bucket_name)
aws s3 rm "s3://${DOCS_BUCKET}" --recursive
aws s3 rm "s3://${DEBUG_BUCKET}" --recursive

terraform destroy
```

Prod has `deletion_protection = true` on the RDS instance. To genuinely
destroy prod, first `terraform apply -var-file=...` with the protection
flipped — never destroy prod without an explicit decision record.

## 9. When the dashboard module gets filled in

Today `terraform/modules/cloudfront/` is a deliberate placeholder. When the
genera-system.com integration call lands, follow the README inside that
directory to replace the placeholder. The deploy flow above doesn't change
— you'll just call the new module from each env's `main.tf`.

## 10. Things you'll want to set up alongside

- **Sentry** for application errors. Add `SENTRY_DSN` to the secrets module
  and inject it into the api + worker task definitions. See
  [`observability.md`](observability.md) §"Where Sentry fits".
- **CloudWatch alarms** per [`observability.md`](observability.md). The
  baseline alarms aren't in the Terraform yet — they're per-env tuning and
  the alarm targets (SNS topic, PagerDuty integration, email) need an
  operator decision.
- **Budget alarms** in AWS Budgets — set a monthly threshold at 1.5× expected
  and route to email.

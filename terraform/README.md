# Terraform — Tender Agent infrastructure

Infrastructure-as-code for the Tender Agent backend on AWS. Region defaults to
**eu-west-2 (London)** for UK data residency.

> **Status:** this package is **ready to apply** but **has not been applied**.
> The deploy is gated on three decisions: AWS account access, dashboard domain
> name, and budget approval. See `docs/deploy.md` for the runbook.

## Layout

```
terraform/
├── bootstrap/             One-time: state backend (S3 + DynamoDB) and ECR repo.
│                          Applied locally with no remote backend before any env.
├── modules/
│   ├── vpc/               VPC + public/private subnets across 2 AZs, IGW, NAT.
│   ├── rds/               Postgres 16 with pgvector, encrypted at rest, managed
│   │                      master password.
│   ├── ecs_service/       Reusable Fargate service module. Used twice per env
│   │                      (api with ALB target group, worker without).
│   ├── s3/                Two buckets: documents (versioned, glacier after 1y)
│   │                      and debug (versioned, deleted after 90d).
│   ├── secrets/           Secrets Manager scaffold for ANTHROPIC_API_KEY,
│   │                      VAPID keypair, and per-portal credentials (Phase 4).
│   │                      Operators populate the values post-apply.
│   ├── alb/               Application Load Balancer in front of the api service.
│   │                      HTTPS optional (set certificate_arn for prod).
│   └── cloudfront/        PLACEHOLDER — see README inside. The dashboard is
│                          planned to be served via genera-system.com integration,
│                          not its own CloudFront distribution. This module is
│                          intentionally empty + flagged so the integration call
│                          is obvious when the time comes.
└── envs/
    ├── staging/           Smaller instance sizes, single NAT, no HTTPS by default.
    └── prod/              Larger sizes, multi-AZ RDS, HTTPS required.
```

## Conventions

- **Region**: `eu-west-2` default; overrideable via `aws_region` tfvar.
- **State**: S3 backend + DynamoDB lock. Bootstrapped separately (`terraform/bootstrap/`).
- **Tags**: every resource gets `project=tender-agent`, `environment=<env>`,
  `managed-by=terraform`. Modules accept an `additional_tags` map for per-env
  extras.
- **Secrets**: ARN-only at the Terraform layer. Operators load real values into
  Secrets Manager out-of-band (`aws secretsmanager put-secret-value …`), per
  the runbook.
- **No state files in git.** `.gitignore` already excludes `*.tfstate*`.
- **No real `tfvars` in git.** Only `terraform.tfvars.example` is committed.

## Quickstart pointers

- First-time setup, walk-through: [`docs/deploy.md`](../docs/deploy.md).
- What to monitor + alarm on: [`docs/observability.md`](../docs/observability.md).
- Container image build path (ECR push on merge to main):
  `.github/workflows/build-and-push-image.yml`.

## Validating without applying

```bash
cd terraform
terraform fmt -check -recursive

for d in bootstrap modules/* envs/*; do
  echo "--- $d ---"
  (cd "$d" && terraform init -backend=false -input=false >/dev/null && terraform validate)
done
```

(Both commands also gate the `Terraform` CI job — see `.github/workflows/`.)

## When to revisit

- **Dashboard module** — once the genera-system.com integration shape is known.
- **NAT redundancy** — staging uses one NAT for cost; prod uses one per AZ.
  Revisit if staging NAT cost is fine and you want HA there too.
- **RDS read replica** — not currently provisioned. Add when read load
  warrants it.
- **Multi-region** — out of scope. UK data residency requires eu-west-2 only.

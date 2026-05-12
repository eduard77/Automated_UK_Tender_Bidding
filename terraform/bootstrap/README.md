# Bootstrap

One-time setup. Provisions the resources every subsequent Terraform run depends
on:

- **S3 bucket** for remote state (one per repo, both envs share it under
  different key prefixes).
- **DynamoDB table** for state locking.
- **ECR repository** for the backend container image.

This module uses a **local backend** because the S3 backend it provisions
doesn't exist yet. Run it once, by hand, before any `envs/` apply.

## Apply

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

Capture the outputs — `tfstate_bucket`, `tfstate_lock_table`, `ecr_repository_url`
— and wire them into `terraform/envs/<env>/backend.tf` if you change the defaults.

## State

The bootstrap module's own state stays **local** to the operator's workstation
(or your CI runner's `bootstrap.tfstate` artifact, if you ever automate this).
That's deliberate: bootstrapping the remote state into the remote state would
be a chicken-and-egg.

Back up `bootstrap/terraform.tfstate` somewhere outside the repo. Without it,
re-creating the bootstrap requires importing the existing resources.

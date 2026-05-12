terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# This module creates the SECRET CONTAINERS only. Values are populated
# out-of-band by operators via `aws secretsmanager put-secret-value` so the
# real keys never appear in Terraform state, the plan, or version control.
# See modules/secrets/README.md and docs/deploy.md for the post-apply
# population steps.

locals {
  base_tags = merge(var.tags, {
    project     = "tender-agent"
    environment = var.environment
    managed-by  = "terraform"
  })

  name_prefix = "tender-agent-${var.environment}"
}

# ---------------------------------------------------------------------------
# Anthropic API key — used by api (requirements extraction) and worker
# (Phase 3 claims extraction). Both services need read access.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "anthropic_api_key" {
  name                    = "${local.name_prefix}/anthropic-api-key"
  description             = "Anthropic API key. Consumers: api (requirements extractor), worker (Phase 3 claims extraction)."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "anthropic-api-key"
    consumers = "api,worker"
  })
}

# ---------------------------------------------------------------------------
# VAPID — three separate secrets so each value can be rotated independently
# of the others (private key rotation is the common case; the public key and
# subject are paired but rarely change).
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "vapid_private_key" {
  name                    = "${local.name_prefix}/vapid-private-key"
  description             = "VAPID private key for Web Push dispatch. Consumer: api (services/push.py)."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "vapid-private-key"
    consumers = "api"
  })
}

resource "aws_secretsmanager_secret" "vapid_public_key" {
  name                    = "${local.name_prefix}/vapid-public-key"
  description             = "VAPID public key, served to the dashboard via GET /push/vapid-public-key. Consumer: api."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "vapid-public-key"
    consumers = "api"
  })
}

resource "aws_secretsmanager_secret" "vapid_subject" {
  name                    = "${local.name_prefix}/vapid-subject"
  description             = "VAPID subject (mailto:operator@example.com or https URL). Consumer: api."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "vapid-subject"
    consumers = "api"
  })
}

# ---------------------------------------------------------------------------
# Dashboard session secret — placeholder. Not consumed by any current code
# path but the slot is created so the secret-population workflow is
# uniformly addressable when session-signing arrives.
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "dashboard_session_secret" {
  name                    = "${local.name_prefix}/dashboard-session-secret"
  description             = "Reserved for future dashboard session signing. Consumer: api (when wired)."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "dashboard-session-secret"
    consumers = "api"
  })
}

# ---------------------------------------------------------------------------
# Portal credentials placeholder — documents the shape Phase 4 portal
# adapters will follow. Each real portal gets its own secret with name
# `${local.name_prefix}/portal/<portal-key>` and value
#     {"username": "...", "password": "...", "totp_seed": "..." (optional)}.
# The placeholder is created tagged-but-empty so:
#   - the consumers tag taxonomy already exists when Phase 4 lands
#   - operators see one example secret in the AWS console matching the
#     final naming convention
# ---------------------------------------------------------------------------
resource "aws_secretsmanager_secret" "portal_credentials_placeholder" {
  name                    = "${local.name_prefix}/portal-credentials-placeholder"
  description             = "DO-NOT-POPULATE. Documents Phase 4 portal credential shape: one secret per portal under tender-agent-${var.environment}/portal/<key>, value JSON {username, password, totp_seed?}."
  kms_key_id              = var.kms_key_id
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(local.base_tags, {
    purpose   = "portal-credentials-placeholder"
    consumers = "none"
    phase     = "4"
  })
}

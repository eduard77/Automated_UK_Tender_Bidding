output "anthropic_api_key_arn" {
  description = "Secrets Manager ARN for the Anthropic API key. Inject as env var ANTHROPIC_API_KEY into api + worker tasks."
  value       = aws_secretsmanager_secret.anthropic_api_key.arn
}

output "vapid_private_key_arn" {
  value = aws_secretsmanager_secret.vapid_private_key.arn
}

output "vapid_public_key_arn" {
  value = aws_secretsmanager_secret.vapid_public_key.arn
}

output "vapid_subject_arn" {
  value = aws_secretsmanager_secret.vapid_subject.arn
}

output "dashboard_session_secret_arn" {
  value = aws_secretsmanager_secret.dashboard_session_secret.arn
}

output "portal_credentials_placeholder_arn" {
  value = aws_secretsmanager_secret.portal_credentials_placeholder.arn
}

# Convenience: every ARN this module manages, suitable as `Resource = [...]`
# in an IAM policy that needs read access to the whole set.
output "all_arns" {
  description = "Every secret ARN managed by this module."
  value = [
    aws_secretsmanager_secret.anthropic_api_key.arn,
    aws_secretsmanager_secret.vapid_private_key.arn,
    aws_secretsmanager_secret.vapid_public_key.arn,
    aws_secretsmanager_secret.vapid_subject.arn,
    aws_secretsmanager_secret.dashboard_session_secret.arn,
    aws_secretsmanager_secret.portal_credentials_placeholder.arn,
  ]
}

# Map keyed by the env var name an ECS task would mount the secret as.
# Lets the env config iterate without repeating each pair.
output "arns_by_env_var" {
  description = "Map of canonical env-var name -> Secrets Manager ARN, for ECS task definition `secrets =` blocks."
  value = {
    ANTHROPIC_API_KEY        = aws_secretsmanager_secret.anthropic_api_key.arn
    VAPID_PRIVATE_KEY        = aws_secretsmanager_secret.vapid_private_key.arn
    VAPID_PUBLIC_KEY         = aws_secretsmanager_secret.vapid_public_key.arn
    VAPID_SUBJECT            = aws_secretsmanager_secret.vapid_subject.arn
    DASHBOARD_SESSION_SECRET = aws_secretsmanager_secret.dashboard_session_secret.arn
  }
}

output "alb_dns_name" {
  description = "Public DNS of the staging ALB. CNAME this from your dev hostname."
  value       = module.alb.alb_dns_name
}

output "alb_zone_id" {
  value = module.alb.alb_zone_id
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "api_service_name" {
  value = module.api_service.service_name
}

output "worker_service_name" {
  value = module.worker_service.service_name
}

output "rds_endpoint" {
  value = module.rds.endpoint
}

output "rds_master_secret_arn" {
  value = module.rds.master_user_secret_arn
}

output "documents_bucket_name" {
  value = module.s3.documents_bucket_name
}

output "debug_bucket_name" {
  value = module.s3.debug_bucket_name
}

output "anthropic_secret_arn" {
  value = module.secrets.anthropic_api_key_arn
}

# VAPID is now three separate secrets (private/public/subject) so each can
# be rotated independently — see modules/secrets/README.md.
output "vapid_private_key_secret_arn" {
  value = module.secrets.vapid_private_key_arn
}

output "vapid_public_key_secret_arn" {
  value = module.secrets.vapid_public_key_arn
}

output "vapid_subject_secret_arn" {
  value = module.secrets.vapid_subject_arn
}

output "dashboard_session_secret_arn" {
  value = module.secrets.dashboard_session_secret_arn
}

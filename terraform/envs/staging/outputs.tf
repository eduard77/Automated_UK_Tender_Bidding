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

output "vapid_secret_arn" {
  value = module.secrets.vapid_keys_arn
}

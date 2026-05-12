output "endpoint" {
  description = "Hostname for connections."
  value       = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "username" {
  value = aws_db_instance.this.username
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN for the RDS-managed master password. Inject into task secrets."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "security_group_id" {
  description = "DB security group ID — apps add ingress against this."
  value       = aws_security_group.db.id
}

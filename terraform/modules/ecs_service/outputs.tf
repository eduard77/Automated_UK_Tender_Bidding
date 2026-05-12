output "service_name" {
  value = aws_ecs_service.this.name
}

output "service_arn" {
  value = aws_ecs_service.this.id
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}

output "security_group_id" {
  description = "Service SG — add as a source on the RDS SG (and any other inbound deps)."
  value       = aws_security_group.service.id
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.this.name
}

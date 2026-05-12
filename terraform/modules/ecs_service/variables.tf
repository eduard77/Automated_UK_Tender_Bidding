variable "name" {
  description = "Env name prefix."
  type        = string
}

variable "service_name" {
  description = "Service name (e.g. \"api\" or \"worker\")."
  type        = string
}

variable "cluster_id" {
  description = "ECS cluster ID (or ARN)."
  type        = string
}

variable "image" {
  description = "Container image, including tag — typically <ecr_url>:<sha>."
  type        = string
}

variable "command" {
  description = "Container command override. Empty list uses the image default."
  type        = list(string)
  default     = []
}

variable "cpu" {
  description = "Task CPU units (256, 512, 1024, ...)."
  type        = number
  default     = 512
}

variable "memory" {
  description = "Task memory in MiB."
  type        = number
  default     = 1024
}

variable "desired_count" {
  description = "Initial task count. Subsequent changes are ignored — use auto-scaling or a separate manual command."
  type        = number
  default     = 1
}

variable "container_port" {
  description = "Container port. Only used when alb_security_group_id is set."
  type        = number
  default     = 8000
}

variable "health_check_path" {
  description = "Path used by both the in-container health check and the ALB target group."
  type        = string
  default     = "/health"
}

variable "vpc_id" {
  description = "VPC the service runs in."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs the tasks attach to."
  type        = list(string)
}

variable "alb_security_group_id" {
  description = "ALB security group ID. Null for non-public services like the worker."
  type        = string
  default     = null
}

variable "target_group_arn" {
  description = "ALB target group ARN. Null for non-public services."
  type        = string
  default     = null
}

variable "environment" {
  description = "Map of env vars baked into the task definition."
  type        = map(string)
  default     = {}
}

variable "secrets" {
  description = "Map of env var name -> Secrets Manager ARN. Resolved at task start."
  type        = map(string)
  default     = {}
}

variable "secret_arns" {
  description = "Full list of secret ARNs the execution role needs to read. Usually values(var.secrets) plus any read-only references."
  type        = list(string)
  default     = []
}

variable "task_policy_json" {
  description = "Extra IAM policy (JSON string) attached to the task role. Use for S3 access, etc."
  type        = string
  default     = null
}

variable "log_retention_days" {
  description = "CloudWatch log retention. 90 days is the default per docs/observability.md."
  type        = number
  default     = 90
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

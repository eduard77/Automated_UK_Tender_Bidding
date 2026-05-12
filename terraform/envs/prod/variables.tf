variable "aws_region" {
  description = "AWS region. UK data residency = eu-west-2."
  type        = string
  default     = "eu-west-2"
}

variable "environment" {
  description = "Environment name. Used in resource prefixes + tags."
  type        = string
  default     = "prod"
}

variable "api_image" {
  description = "Fully-qualified container image (ECR url:tag). Populated by the build-and-push workflow."
  type        = string
}

variable "documents_bucket_name" {
  description = "Globally-unique name for the tender documents bucket."
  type        = string
  default     = "tender-agent-prod-documents"
}

variable "debug_bucket_name" {
  description = "Globally-unique name for the portal debug bucket."
  type        = string
  default     = "tender-agent-prod-debug"
}

variable "alb_certificate_arn" {
  description = "ACM cert ARN for HTTPS. REQUIRED in prod — the cert must already exist."
  type        = string
}

variable "api_desired_count" {
  description = "Initial api task count. Prod should run at least 2 for HA."
  type        = number
  default     = 2
}

variable "worker_desired_count" {
  description = "Initial worker task count. One is typically enough; bump if poll backlog grows."
  type        = number
  default     = 1
}

variable "aws_region" {
  description = "AWS region. UK data residency = eu-west-2."
  type        = string
  default     = "eu-west-2"
}

variable "environment" {
  description = "Environment name. Used in resource prefixes + tags."
  type        = string
  default     = "staging"
}

variable "api_image" {
  description = "Fully-qualified container image (ECR url:tag). Populated by the build-and-push workflow."
  type        = string
}

variable "documents_bucket_name" {
  description = "Globally-unique name for the tender documents bucket."
  type        = string
  default     = "tender-agent-staging-documents"
}

variable "debug_bucket_name" {
  description = "Globally-unique name for the portal debug bucket."
  type        = string
  default     = "tender-agent-staging-debug"
}

variable "alb_certificate_arn" {
  description = "ACM cert ARN for HTTPS. Null in staging means HTTP-only (the ALB will only listen on :80)."
  type        = string
  default     = null
}

variable "api_desired_count" {
  description = "Initial api task count."
  type        = number
  default     = 1
}

variable "worker_desired_count" {
  description = "Initial worker task count."
  type        = number
  default     = 1
}

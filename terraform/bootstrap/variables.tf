variable "aws_region" {
  description = "AWS region. UK data residency dictates eu-west-2 by default."
  type        = string
  default     = "eu-west-2"
}

variable "tfstate_bucket_name" {
  description = "S3 bucket for remote Terraform state. Must be globally unique."
  type        = string
  default     = "tender-agent-tfstate"
}

variable "tfstate_lock_table_name" {
  description = "DynamoDB table for Terraform state locking."
  type        = string
  default     = "tender-agent-tfstate-lock"
}

variable "ecr_repository_name" {
  description = "ECR repository for the backend container image."
  type        = string
  default     = "tender-agent-api"
}

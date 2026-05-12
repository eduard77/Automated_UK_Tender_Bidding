variable "name" {
  description = "Resource name prefix (typically env name)."
  type        = string
}

variable "vpc_id" {
  description = "VPC the DB lives in."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs (at least 2, in different AZs, for multi-AZ)."
  type        = list(string)
}

variable "allowed_security_group_ids" {
  description = "Security group IDs that should be allowed to reach the DB on 5432."
  type        = list(string)
}

variable "engine_version" {
  description = "Postgres engine version. pgvector is available on RDS Postgres 16+."
  type        = string
  default     = "16.4"
}

variable "instance_class" {
  description = "Instance class (db.t4g.* for staging, db.m6g.* for prod)."
  type        = string
  default     = "db.t4g.medium"
}

variable "allocated_storage" {
  description = "Initial storage in GiB."
  type        = number
  default     = 50
}

variable "max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 500
}

variable "db_name" {
  description = "Initial database name."
  type        = string
  default     = "tender_agent"
}

variable "username" {
  description = "Master username. Password is managed by RDS in Secrets Manager."
  type        = string
  default     = "tender"
}

variable "multi_az" {
  description = "Enable multi-AZ HA. False in staging, true in prod."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention in days."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Block accidental terraform destroy. True in prod."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

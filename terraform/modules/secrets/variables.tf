variable "environment" {
  description = "Environment name (e.g. \"staging\" or \"prod\"). Used in secret names and the `environment` tag."
  type        = string
}

variable "kms_key_id" {
  description = "KMS key (ID, ARN, or alias/name) for envelope-encrypting the secret values. Null falls back to the AWS-managed `aws/secretsmanager` key — adequate for non-regulated workloads."
  type        = string
  default     = null
}

variable "recovery_window_in_days" {
  description = "Days a deleted secret stays recoverable before permanent deletion. Set 0 in staging to allow immediate re-create; 7-30 in prod."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Additional tags merged on top of project/environment/managed-by. Module also adds per-secret `purpose` and `consumers` tags."
  type        = map(string)
  default     = {}
}

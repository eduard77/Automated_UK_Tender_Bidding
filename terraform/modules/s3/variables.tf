variable "documents_bucket_name" {
  description = "Bucket name for tender documents. Must be globally unique."
  type        = string
}

variable "debug_bucket_name" {
  description = "Bucket name for portal debug artifacts (Phase 4). Must be globally unique."
  type        = string
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

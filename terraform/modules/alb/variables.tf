variable "name" {
  description = "Resource name prefix (typically env name)."
  type        = string
}

variable "vpc_id" {
  description = "VPC the ALB lives in."
  type        = string
}

variable "public_subnet_ids" {
  description = "Public subnet IDs for the ALB (at least 2 AZs)."
  type        = list(string)
}

variable "container_port" {
  description = "Port the api container listens on."
  type        = number
  default     = 8000
}

variable "health_check_path" {
  description = "HTTP path used for ALB health checks against the api service."
  type        = string
  default     = "/health"
}

variable "certificate_arn" {
  description = "ACM cert ARN for HTTPS. Null in staging means HTTP-only (do NOT use in prod)."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

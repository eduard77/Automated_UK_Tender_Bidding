variable "name" {
  description = "Name prefix used on every resource (typically env name)."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. /16 recommended."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of AZs to spread subnets across. 2 is the minimum for HA RDS."
  type        = number
  default     = 2
}

variable "single_nat" {
  description = "Use a single NAT Gateway shared by all private subnets. Saves ~30 GBP/mo per AZ but loses HA for egress. OK for staging, not prod."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource."
  type        = map(string)
  default     = {}
}

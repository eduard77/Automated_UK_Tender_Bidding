terraform {
  required_version = ">= 1.6.0"
}

# This module is intentionally a placeholder. See README.md in this directory.
#
# The dashboard's hosting target hasn't been decided (genera-system.com
# integration shape is TBD). Until then, instantiating this module from an env
# is a deliberate error so the next operator knows the integration call still
# needs to be made.
#
# To resolve: replace the `terraform_data` below with the real CloudFront
# distribution, origin config, ACM cert, and Route53 record once the call
# lands. Or delete the module entirely if the dashboard ends up hosted outside
# of this Terraform scope.

variable "dashboard_origin_domain_name" {
  description = "Dashboard origin (S3 bucket website endpoint, ALB DNS, or Vercel/Netlify origin). Leave unset — module is a placeholder."
  type        = string
  default     = null
}

resource "terraform_data" "placeholder" {
  triggers_replace = [
    "dashboard-hosting-undecided",
  ]
  provisioner "local-exec" {
    command = <<-EOT
      echo ""
      echo "================================================================"
      echo "  modules/cloudfront/ is a placeholder."
      echo "  Dashboard hosting is gated on the genera-system.com integration"
      echo "  call. See terraform/modules/cloudfront/README.md."
      echo "================================================================"
      echo ""
      exit 1
    EOT
  }
}

output "status" {
  value = "placeholder — see terraform/modules/cloudfront/README.md"
}

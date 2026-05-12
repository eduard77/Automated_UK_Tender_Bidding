output "alb_arn" {
  value = aws_lb.this.arn
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB. Wire into Route53 / CNAME records."
  value       = aws_lb.this.dns_name
}

output "alb_zone_id" {
  description = "Hosted-zone ID of the ALB, for Route53 alias records."
  value       = aws_lb.this.zone_id
}

output "target_group_arn" {
  description = "Target group the api ECS service registers IPs against."
  value       = aws_lb_target_group.api.arn
}

output "security_group_id" {
  description = "ALB SG — apps add ingress against this on the container port."
  value       = aws_security_group.alb.id
}

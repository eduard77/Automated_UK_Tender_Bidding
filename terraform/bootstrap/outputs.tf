output "tfstate_bucket" {
  description = "S3 bucket for remote state. Wire into envs/<env>/backend.tf."
  value       = aws_s3_bucket.tfstate.bucket
}

output "tfstate_lock_table" {
  description = "DynamoDB table for state locking. Wire into envs/<env>/backend.tf."
  value       = aws_dynamodb_table.tfstate_lock.name
}

output "ecr_repository_url" {
  description = "Full URL for the ECR repo. Used by the build-and-push workflow."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repo. Used in IAM policies for the GHA OIDC role."
  value       = aws_ecr_repository.api.arn
}

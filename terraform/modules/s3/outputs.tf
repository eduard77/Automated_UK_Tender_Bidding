output "documents_bucket_name" {
  value = aws_s3_bucket.documents.bucket
}

output "documents_bucket_arn" {
  value = aws_s3_bucket.documents.arn
}

output "debug_bucket_name" {
  value = aws_s3_bucket.debug.bucket
}

output "debug_bucket_arn" {
  value = aws_s3_bucket.debug.arn
}

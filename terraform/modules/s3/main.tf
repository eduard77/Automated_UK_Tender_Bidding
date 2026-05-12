terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# ---------------------------------------------------------------------------
# Documents bucket — long-lived storage for tender ITT/spec attachments.
# Versioned, encrypted, lifecycle to glacier after 1 year. Never public.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "documents" {
  bucket = var.documents_bucket_name
  tags   = merge(var.tags, { Purpose = "tender-documents" })
}

resource "aws_s3_bucket_versioning" "documents" {
  bucket = aws_s3_bucket.documents.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "documents" {
  bucket                  = aws_s3_bucket.documents.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "documents" {
  bucket = aws_s3_bucket.documents.id

  rule {
    id     = "glacier-after-1y"
    status = "Enabled"
    filter {}
    transition {
      days          = 365
      storage_class = "GLACIER"
    }
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}

# ---------------------------------------------------------------------------
# Debug bucket — Phase 4 portal screenshots + HAR captures. Short-lived.
# Versioned (so we can compare snapshots across runs), expired after 90 days.
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "debug" {
  bucket = var.debug_bucket_name
  tags   = merge(var.tags, { Purpose = "portal-debug" })
}

resource "aws_s3_bucket_versioning" "debug" {
  bucket = aws_s3_bucket.debug.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "debug" {
  bucket = aws_s3_bucket.debug.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "debug" {
  bucket                  = aws_s3_bucket.debug.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "debug" {
  bucket = aws_s3_bucket.debug.id

  rule {
    id     = "expire-after-90d"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

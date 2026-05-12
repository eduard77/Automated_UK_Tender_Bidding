locals {
  name = "tender-agent-${var.environment}"
  tags = {
    project     = "tender-agent"
    environment = var.environment
    managed-by  = "terraform"
  }
}

module "vpc" {
  source     = "../../modules/vpc"
  name       = local.name
  vpc_cidr   = "10.20.0.0/16"
  az_count   = 2
  single_nat = true # staging: shared NAT to save ~£30/mo
  tags       = local.tags
}

module "s3" {
  source                = "../../modules/s3"
  documents_bucket_name = var.documents_bucket_name
  debug_bucket_name     = var.debug_bucket_name
  tags                  = local.tags
}

module "secrets" {
  source                  = "../../modules/secrets"
  environment             = var.environment
  recovery_window_in_days = 0 # staging: allow immediate recreate
  tags                    = local.tags
}

resource "aws_ecs_cluster" "this" {
  name = "${local.name}-cluster"
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
  tags = local.tags
}

module "alb" {
  source            = "../../modules/alb"
  name              = local.name
  vpc_id            = module.vpc.vpc_id
  public_subnet_ids = module.vpc.public_subnet_ids
  certificate_arn   = var.alb_certificate_arn
  tags              = local.tags
}

# Build the IAM policy for the task role here so we can wire bucket ARNs
# in by reference (cleaner than the module trying to compute them).
data "aws_iam_policy_document" "task_app_access" {
  statement {
    sid    = "S3DocumentsReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
    ]
    resources = [
      module.s3.documents_bucket_arn,
      "${module.s3.documents_bucket_arn}/*",
      module.s3.debug_bucket_arn,
      "${module.s3.debug_bucket_arn}/*",
    ]
  }
}

# Shared env + secrets between api and worker — same image, same config; the
# only difference is the command (and ALB attachment, for api).
locals {
  shared_environment = {
    APP_ENV              = var.environment
    LOG_LEVEL            = "INFO"
    DOCUMENT_STORAGE_DIR = "s3://${module.s3.documents_bucket_name}"
    DASHBOARD_BASE_URL   = "https://dashboard-tbd.example.invalid" # operator overrides post genera-system call
  }
  # Application secrets injected as env vars at task start. Three separate
  # VAPID secrets (rather than a single JSON blob) so private-key rotation
  # doesn't touch the public key or subject. The RDS master password is
  # injected as a JSON blob the container parses on startup.
  shared_secrets = merge(
    module.secrets.arns_by_env_var,
    {
      DATABASE_PASSWORD_JSON = module.rds.master_user_secret_arn
    },
  )
  shared_secret_arns = concat(
    module.secrets.all_arns,
    [module.rds.master_user_secret_arn],
  )
}

module "api_service" {
  source                = "../../modules/ecs_service"
  name                  = local.name
  service_name          = "api"
  cluster_id            = aws_ecs_cluster.this.id
  image                 = var.api_image
  cpu                   = 512
  memory                = 1024
  desired_count         = var.api_desired_count
  vpc_id                = module.vpc.vpc_id
  subnet_ids            = module.vpc.private_subnet_ids
  alb_security_group_id = module.alb.security_group_id
  target_group_arn      = module.alb.target_group_arn
  environment           = local.shared_environment
  secrets               = local.shared_secrets
  secret_arns           = local.shared_secret_arns
  task_policy_json      = data.aws_iam_policy_document.task_app_access.json
  tags                  = local.tags
}

module "worker_service" {
  source           = "../../modules/ecs_service"
  name             = local.name
  service_name     = "worker"
  cluster_id       = aws_ecs_cluster.this.id
  image            = var.api_image
  command          = ["python", "-m", "tender_agent.scheduler"]
  cpu              = 512
  memory           = 1024
  desired_count    = var.worker_desired_count
  vpc_id           = module.vpc.vpc_id
  subnet_ids       = module.vpc.private_subnet_ids
  environment      = local.shared_environment
  secrets          = local.shared_secrets
  secret_arns      = local.shared_secret_arns
  task_policy_json = data.aws_iam_policy_document.task_app_access.json
  tags             = local.tags
}

module "rds" {
  source     = "../../modules/rds"
  name       = local.name
  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnet_ids

  # Both services need DB access. Listing both SGs here.
  allowed_security_group_ids = [
    module.api_service.security_group_id,
    module.worker_service.security_group_id,
  ]

  instance_class        = "db.t4g.medium"
  allocated_storage     = 50
  multi_az              = false # staging: single-AZ
  backup_retention_days = 7
  deletion_protection   = false # staging: allow terraform destroy
  tags                  = local.tags
}

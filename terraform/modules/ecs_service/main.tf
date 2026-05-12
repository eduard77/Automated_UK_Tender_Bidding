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
# IAM
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "task_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Execution role — ECS uses this to pull images + fetch secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-${var.service_name}-exec"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "execution_basic" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Permission to read the listed secrets — execution role needs this so the
# container can be started with `secrets =` references in the task definition.
resource "aws_iam_role_policy" "execution_secrets" {
  count = length(var.secret_arns) > 0 ? 1 : 0
  name  = "secrets-read"
  role  = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.secret_arns
    }]
  })
}

# Task role — what the application code uses (S3, etc.).
resource "aws_iam_role" "task" {
  name               = "${var.name}-${var.service_name}-task"
  assume_role_policy = data.aws_iam_policy_document.task_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "task_extra" {
  count  = var.task_policy_json == null ? 0 : 1
  name   = "app-policy"
  role   = aws_iam_role.task.id
  policy = var.task_policy_json
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${var.name}-${var.service_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------

resource "aws_security_group" "service" {
  name        = "${var.name}-${var.service_name}-sg"
  description = "ECS service SG for ${var.service_name}."
  vpc_id      = var.vpc_id
  tags        = merge(var.tags, { Name = "${var.name}-${var.service_name}-sg" })
}

resource "aws_security_group_rule" "service_ingress_from_alb" {
  count                    = var.alb_security_group_id == null ? 0 : 1
  type                     = "ingress"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.service.id
  source_security_group_id = var.alb_security_group_id
  description              = "${var.container_port} from ALB"
}

resource "aws_security_group_rule" "service_egress" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.service.id
  description       = "all egress"
}

# ---------------------------------------------------------------------------
# Task definition + service
# ---------------------------------------------------------------------------

locals {
  container_def = jsonencode([{
    name      = var.service_name
    image     = var.image
    essential = true
    command   = var.command

    portMappings = var.alb_security_group_id == null ? [] : [{
      containerPort = var.container_port
      hostPort      = var.container_port
      protocol      = "tcp"
    }]

    environment = [for k, v in var.environment : { name = k, value = v }]
    secrets     = [for k, arn in var.secrets : { name = k, valueFrom = arn }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.this.name
        awslogs-region        = data.aws_region.current.name
        awslogs-stream-prefix = var.service_name
      }
    }

    healthCheck = var.alb_security_group_id == null ? null : {
      command     = ["CMD-SHELL", "curl -fsS http://localhost:${var.container_port}${var.health_check_path} || exit 1"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }])
}

data "aws_region" "current" {}

resource "aws_ecs_task_definition" "this" {
  family                   = "${var.name}-${var.service_name}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions    = local.container_def
  tags                     = var.tags
}

resource "aws_ecs_service" "this" {
  name             = "${var.name}-${var.service_name}"
  cluster          = var.cluster_id
  task_definition  = aws_ecs_task_definition.this.arn
  desired_count    = var.desired_count
  launch_type      = "FARGATE"
  platform_version = "LATEST"

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.service.id]
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [1]
    content {
      target_group_arn = var.target_group_arn
      container_name   = var.service_name
      container_port   = var.container_port
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = var.tags
}

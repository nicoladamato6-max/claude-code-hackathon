terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {
    bucket         = "contoso-tfstate"
    key            = "batch/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "contoso-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
  default_tags {
    tags = {
      Project    = "contoso-financial"
      Workload   = "batch-reconciliation"
      ManagedBy  = "terraform"
      CostCenter = "IT-Infrastructure"
    }
  }
}

locals { name_prefix = "contoso-batch-${var.environment}" }

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "batch_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["batch.amazonaws.com", "ecs-tasks.amazonaws.com"] }
  }
}

resource "aws_iam_role" "batch_exec" {
  name               = "${local.name_prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}

resource "aws_iam_role_policy_attachment" "batch_exec_managed" {
  role       = aws_iam_role.batch_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "batch_task" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.db_secret_arn]
  }
  statement {
    actions   = ["s3:PutObject", "s3:GetObject", "s3:HeadObject"]
    resources = ["arn:aws:s3:::${var.s3_bucket_output}/*"]
  }
}

resource "aws_iam_role" "batch_task" {
  name               = "${local.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.batch_assume.json
}

resource "aws_iam_role_policy" "batch_task" {
  name   = "batch-task-policy"
  role   = aws_iam_role.batch_task.id
  policy = data.aws_iam_policy_document.batch_task.json
}

# ---------------------------------------------------------------------------
# Security group — outbound only (connects to RDS and S3)
# ---------------------------------------------------------------------------
resource "aws_security_group" "batch" {
  name   = "${local.name_prefix}-sg"
  vpc_id = var.vpc_id

  egress { from_port = 0; to_port = 0; protocol = "-1"; cidr_blocks = ["0.0.0.0/0"] }
}

# ---------------------------------------------------------------------------
# AWS Batch — Fargate compute environment (ADR-05)
# ---------------------------------------------------------------------------
resource "aws_batch_compute_environment" "reconcile" {
  compute_environment_name = local.name_prefix
  type                     = "MANAGED"
  state                    = "ENABLED"

  compute_resources {
    type               = "FARGATE"
    max_vcpus          = 4
    subnets            = var.private_subnets
    security_group_ids = [aws_security_group.batch.id]
  }
}

resource "aws_batch_job_queue" "reconcile" {
  name     = local.name_prefix
  state    = "ENABLED"
  priority = 1

  compute_environment_order {
    order               = 1
    compute_environment = aws_batch_compute_environment.reconcile.arn
  }
}

resource "aws_batch_job_definition" "reconcile" {
  name = local.name_prefix
  type = "container"

  platform_capabilities = ["FARGATE"]

  timeout { attempt_duration_seconds = var.job_timeout_sec }

  # Fail fast: no automatic retries — AWS Batch FAILED state triggers the CloudWatch alarm
  retry_strategy { attempts = 1 }

  container_properties = jsonencode({
    image            = var.batch_image
    jobRoleArn       = aws_iam_role.batch_task.arn
    executionRoleArn = aws_iam_role.batch_exec.arn

    fargatePlatformConfiguration = { platformVersion = "LATEST" }
    resourceRequirements = [
      { type = "VCPU",   value = "2" },
      { type = "MEMORY", value = "4096" },
    ]

    # DATABASE_URL injected from Secrets Manager — never plaintext (ADR-08)
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.db_secret_arn },
    ]

    environment = [
      { name = "S3_BUCKET_OUTPUT", value = var.s3_bucket_output },
      { name = "AWS_REGION",       value = var.aws_region },
      # JOB_DATE is injected at submit time by EventBridge Scheduler
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/batch/${local.name_prefix}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "reconcile"
      }
    }
  })
}

resource "aws_cloudwatch_log_group" "batch" {
  name              = "/batch/${local.name_prefix}"
  retention_in_days = 90
}

# ---------------------------------------------------------------------------
# EventBridge Scheduler — daily at 02:00 UTC (ADR-06, replaces on-prem cron)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["scheduler.amazonaws.com"] }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler_batch" {
  name = "submit-batch-job"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "batch:SubmitJob"
      Resource = [aws_batch_job_definition.reconcile.arn, aws_batch_job_queue.reconcile.arn]
    }]
  })
}

resource "aws_scheduler_schedule" "nightly" {
  name                         = "${local.name_prefix}-nightly"
  schedule_expression          = var.job_schedule
  schedule_expression_timezone = "Europe/Dublin"   # eu-west-1 local time

  flexible_time_window { mode = "OFF" }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:batch:submitJob"
    role_arn = aws_iam_role.scheduler.arn

    input = jsonencode({
      JobName       = "${local.name_prefix}-nightly"
      JobQueue      = aws_batch_job_queue.reconcile.name
      JobDefinition = aws_batch_job_definition.reconcile.name
      # JOB_DATE injected as today's date at schedule-fire time
      ContainerOverrides = {
        Environment = [
          { Name = "JOB_DATE", Value = "<aws.scheduler.scheduled-time | date: '%Y-%m-%d'>" }
        ]
      }
    })
  }
}

# ---------------------------------------------------------------------------
# CloudWatch alarm — no S3 output by 04:15 triggers SNS (SRE concern #1 + #9)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "batch_no_output" {
  alarm_name          = "${local.name_prefix}-no-output-by-0415"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "NumberOfObjectsCreated"
  namespace           = "AWS/S3"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Batch reconciliation produced no S3 output by 04:15 — SLA breach"
  alarm_actions       = [var.sns_alerts_arn]
  ok_actions          = [var.sns_alerts_arn]

  dimensions = {
    BucketName  = var.s3_bucket_output
    StorageType = "AllStorageTypes"
  }
}

resource "aws_cloudwatch_metric_alarm" "batch_job_failed" {
  alarm_name          = "${local.name_prefix}-job-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FailedJobCount"
  namespace           = "AWS/Batch"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "AWS Batch reconciliation job exited non-zero — Finance team SLA at risk"
  alarm_actions       = [var.sns_alerts_arn]

  dimensions = {
    JobQueue = aws_batch_job_queue.reconcile.name
  }
}

resource "aws_cloudwatch_metric_alarm" "batch_duration" {
  alarm_name          = "${local.name_prefix}-duration-warning"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "SucceededJobCount"
  namespace           = "AWS/Batch"
  period              = 5400   # 90-minute early warning (SLA = 120 min)
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_description   = "Batch job has not succeeded within 90 min — SLA breach risk"
  alarm_actions       = [var.sns_alerts_arn]

  dimensions = {
    JobQueue = aws_batch_job_queue.reconcile.name
  }
}

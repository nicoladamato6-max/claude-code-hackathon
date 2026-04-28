terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Bootstrap: this module must be applied FIRST via local state,
  # then the state bucket created here is used by all other modules.
  backend "s3" {
    bucket         = "contoso-tfstate"
    key            = "shared/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "contoso-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region

  # Cost allocation tags applied to every resource in every module (01-memo.md §Cost analysis)
  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = var.owner
      CostCenter  = var.cost_center
    }
  }
}

locals { name_prefix = "${var.project}-${var.environment}" }

# ---------------------------------------------------------------------------
# Terraform state backend (apply once with local state, then migrate)
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "tfstate" {
  bucket = "contoso-tfstate"
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tfstate_lock" {
  name         = "contoso-tfstate-lock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute { name = "LockID"; type = "S" }
}

# ---------------------------------------------------------------------------
# ECR repositories — one per containerised workload
# ---------------------------------------------------------------------------
resource "aws_ecr_repository" "web_app" {
  name                 = "${local.name_prefix}/web-app"
  image_tag_mutability = "IMMUTABLE"   # prevents overwriting released tags

  image_scanning_configuration { scan_on_push = true }  # detect vulnerabilities on push
}

resource "aws_ecr_repository" "batch" {
  name                 = "${local.name_prefix}/batch-reconciliation"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration { scan_on_push = true }
}

# Lifecycle policy: keep the last 10 production images, expire untagged within 7 days
resource "aws_ecr_lifecycle_policy" "web_app" {
  repository = aws_ecr_repository.web_app.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 7 days"
        selection    = { tagStatus = "untagged"; countType = "sinceImagePushed"; countUnit = "days"; countNumber = 7 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep last 10 tagged releases"
        selection    = { tagStatus = "tagged"; tagPrefixList = ["v"]; countType = "imageCountMoreThan"; countNumber = 10 }
        action       = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "batch" {
  repository = aws_ecr_repository.batch.name
  policy     = aws_ecr_lifecycle_policy.web_app.policy
}

# ---------------------------------------------------------------------------
# S3 application buckets (all eu-west-1, encrypted, versioned)
# ---------------------------------------------------------------------------
locals {
  app_buckets = {
    web_assets             = "contoso-web-assets-${var.environment}"
    reconciliation_output  = "contoso-reconciliation-output-${var.environment}"
    db_backups             = "contoso-db-backups-${var.environment}"
  }
}

resource "aws_s3_bucket" "app" {
  for_each = local.app_buckets
  bucket   = each.value
}

resource "aws_s3_bucket_versioning" "app" {
  for_each = local.app_buckets
  bucket   = aws_s3_bucket.app[each.key].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  for_each = local.app_buckets
  bucket   = aws_s3_bucket.app[each.key].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "app" {
  for_each                = local.app_buckets
  bucket                  = aws_s3_bucket.app[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Reconciliation output: expire daily prefixes after 90 days (GDPR + cost)
resource "aws_s3_bucket_lifecycle_configuration" "reconciliation" {
  bucket = aws_s3_bucket.app["reconciliation_output"].id

  rule {
    id     = "expire-old-reports"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 90 }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

# DB backups: keep 365 days (compliance requirement)
resource "aws_s3_bucket_lifecycle_configuration" "db_backups" {
  bucket = aws_s3_bucket.app["db_backups"].id

  rule {
    id     = "transition-to-glacier"
    status = "Enabled"
    filter { prefix = "" }
    transition { days = 30; storage_class = "GLACIER" }
    expiration { days = 365 }
  }
}

# ---------------------------------------------------------------------------
# SNS topic — single fanout for all CloudWatch alarms across workloads
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  name              = "${local.name_prefix}-alerts"
  kms_master_key_id = "alias/aws/sns"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ---------------------------------------------------------------------------
# CloudTrail — API-level audit log (GDPR art.32 + EBA cloud outsourcing guidelines)
# All AWS API calls logged to S3 with log file validation enabled.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "cloudtrail" {
  bucket = "${local.name_prefix}-cloudtrail-logs"
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket                  = aws_s3_bucket.cloudtrail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  rule {
    id     = "cloudtrail-retention"
    status = "Enabled"
    filter { prefix = "" }
    transition { days = 90;  storage_class = "GLACIER" }
    expiration { days = 365 }   # 1-year audit retention
  }
}

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AWSCloudTrailAclCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.cloudtrail.arn
      },
      {
        Sid       = "AWSCloudTrailWrite"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.cloudtrail.arn}/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = { StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" } }
      }
    ]
  })
}

resource "aws_cloudtrail" "main" {
  name                          = "${local.name_prefix}-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  include_global_service_events = true
  is_multi_region_trail         = false   # eu-west-1 only — GDPR data residency
  enable_log_file_validation    = true    # detects log tampering

  event_selector {
    read_write_type           = "All"
    include_management_events = true
    data_resource {
      type   = "AWS::S3::Object"
      values = ["arn:aws:s3:::"]   # all S3 object events
    }
  }

  depends_on = [aws_s3_bucket_policy.cloudtrail]
}

# ---------------------------------------------------------------------------
# AWS Budgets — alerts when spend exceeds €350/month estimate (01-memo.md TCO)
# ---------------------------------------------------------------------------
resource "aws_budgets_budget" "monthly" {
  name         = "${local.name_prefix}-monthly-spend"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_sns_topic_arns  = [aws_sns_topic.alerts.arn]
  }
}

# ---------------------------------------------------------------------------
# Outputs consumed by other modules
# ---------------------------------------------------------------------------
output "sns_alerts_arn"                  { value = aws_sns_topic.alerts.arn }
output "ecr_web_app_url"                 { value = aws_ecr_repository.web_app.repository_url }
output "ecr_batch_url"                   { value = aws_ecr_repository.batch.repository_url }
output "s3_web_assets_bucket"            { value = aws_s3_bucket.app["web_assets"].id }
output "s3_reconciliation_output_bucket" { value = aws_s3_bucket.app["reconciliation_output"].id }
output "s3_db_backups_bucket"            { value = aws_s3_bucket.app["db_backups"].id }

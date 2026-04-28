terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {
    bucket         = "contoso-tfstate"
    key            = "reporting/terraform.tfstate"
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
      Workload   = "reporting-db"
      ManagedBy  = "terraform"
      CostCenter = "IT-Infrastructure"
    }
  }
}

locals { name_prefix = "contoso-reporting-${var.environment}" }

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
resource "aws_security_group" "rds" {
  name   = "${local.name_prefix}-rds"
  vpc_id = var.vpc_id

  # Only accept connections from known app security groups — replaces 0.0.0.0/0 in pg_hba.conf
  dynamic "ingress" {
    for_each = var.allowed_app_sg_ids
    content {
      from_port       = 5432
      to_port         = 5432
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }
}

resource "aws_security_group" "redis" {
  name   = "${local.name_prefix}-redis"
  vpc_id = var.vpc_id

  dynamic "ingress" {
    for_each = var.allowed_app_sg_ids
    content {
      from_port       = 6379
      to_port         = 6379
      protocol        = "tcp"
      security_groups = [ingress.value]
    }
  }
}

# ---------------------------------------------------------------------------
# RDS subnet group — must span at least 2 AZs for Multi-AZ (ADR-03)
# ---------------------------------------------------------------------------
resource "aws_db_subnet_group" "main" {
  name       = local.name_prefix
  subnet_ids = var.private_subnets
}

# ---------------------------------------------------------------------------
# RDS parameter group — enables pgaudit (GDPR art. 32, ADR-08, discovery §5)
# ---------------------------------------------------------------------------
resource "aws_db_parameter_group" "pg15" {
  name   = "${local.name_prefix}-pg15"
  family = "postgres15"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_cron,pgaudit"
  }
  parameter {
    name  = "pgaudit.log"
    value = "ddl,write,role"
  }
  parameter {
    name  = "pgaudit.log_relation"
    value = "on"
  }
  parameter {
    name  = "cron.database_name"
    value = var.db_name
  }
  # Connection timeout — prevents batch job from hanging on network partition (SRE concern #9)
  parameter {
    name  = "tcp_keepalives_idle"
    value = "60"
  }
}

# ---------------------------------------------------------------------------
# RDS PostgreSQL 15 Multi-AZ (ADR-02 + ADR-03)
# Single instance, separate schemas per workload (ADR-11)
# ---------------------------------------------------------------------------
resource "aws_db_instance" "main" {
  identifier     = local.name_prefix
  engine         = "postgres"
  engine_version = "15"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_gb
  max_allocated_storage = 500        # autoscaling up to 500 GB
  storage_type          = "gp3"
  storage_encrypted     = true       # encryption at rest (GDPR)

  db_name  = var.db_name
  username = "contoso_admin"
  # Password managed by Secrets Manager with auto-rotation — never hardcoded
  manage_master_user_password   = true
  master_user_secret_kms_key_id = data.aws_kms_key.rds.arn

  multi_az               = true      # ADR-03: automated failover < 60s
  db_subnet_group_name   = aws_db_subnet_group.main.name
  parameter_group_name   = aws_db_parameter_group.pg15.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  backup_retention_period   = 14     # 14-day point-in-time recovery (SRE concern #3)
  backup_window             = "03:00-04:00"
  maintenance_window        = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true  # SRE concern #4: patching without SSH

  deletion_protection = true         # prevents accidental terraform destroy
  skip_final_snapshot = false
  final_snapshot_identifier = "${local.name_prefix}-final"

  performance_insights_enabled          = true   # ADR-09: RDS Performance Insights
  performance_insights_retention_period = 7
  monitoring_interval                   = 60     # Enhanced Monitoring every 60s
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
}

data "aws_kms_key" "rds" {
  key_id = "alias/aws/rds"
}

resource "aws_cloudwatch_log_group" "rds_postgres" {
  name              = "/aws/rds/instance/${local.name_prefix}/postgresql"
  retention_in_days = 90   # GDPR 90-day hot retention
}

# Enhanced Monitoring IAM role
data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["monitoring.rds.amazonaws.com"] }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name_prefix}-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# ---------------------------------------------------------------------------
# RDS Read Replica — serves Risk/Finance concurrent queries (ADR-11, SRE concern #2)
# ---------------------------------------------------------------------------
resource "aws_db_instance" "replica" {
  identifier             = "${local.name_prefix}-replica"
  replicate_source_db    = aws_db_instance.main.identifier
  instance_class         = var.db_instance_class
  publicly_accessible    = false
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.pg15.name

  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.rds_monitoring.arn
  auto_minor_version_upgrade   = true
}

# ---------------------------------------------------------------------------
# ElastiCache Redis — AOF persistence + replication (ADR-04, SRE concern #7)
# ---------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name_prefix}-redis"
  subnet_ids = var.private_subnets
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name_prefix}-redis"
  description                = "Session store for web-app — AOF persistence enabled"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 2      # primary + one replica for Multi-AZ failover
  automatic_failover_enabled = true
  multi_az_enabled           = true
  subnet_group_name          = aws_elasticache_subnet_group.redis.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true   # GDPR encryption at rest
  transit_encryption_enabled = true   # GDPR encryption in transit

  # AOF persistence: survives restarts without losing sessions (SRE concern #7)
  parameter_group_name = aws_elasticache_parameter_group.redis.name

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }
}

resource "aws_elasticache_parameter_group" "redis" {
  name   = "${local.name_prefix}-redis7"
  family = "redis7"

  parameter { name = "appendonly";     value = "yes" }   # AOF persistence
  parameter { name = "appendfsync";    value = "everysec" }
}

resource "aws_cloudwatch_log_group" "redis" {
  name              = "/elasticache/${local.name_prefix}/slow-log"
  retention_in_days = 30
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# AWS Backup — automated RDS snapshots with retention policy (SRE concern #3)
# Complements RDS automated backups with a separate, auditable backup plan.
# ---------------------------------------------------------------------------
resource "aws_backup_vault" "main" {
  name        = "${local.name_prefix}-vault"
  kms_key_arn = data.aws_kms_key.rds.arn
}

resource "aws_backup_plan" "rds" {
  name = "${local.name_prefix}-rds-backup"

  rule {
    rule_name         = "daily-backup"
    target_vault_name = aws_backup_vault.main.name
    schedule          = "cron(0 5 * * ? *)"   # 05:00 UTC, after batch window

    lifecycle {
      cold_storage_after = 30    # move to Glacier after 30 days
      delete_after       = 365   # comply with 1-year financial record retention
    }
  }
}

data "aws_iam_policy_document" "backup_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["backup.amazonaws.com"] }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${local.name_prefix}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_backup_selection" "rds" {
  iam_role_arn = aws_iam_role.backup.arn
  name         = "rds-selection"
  plan_id      = aws_backup_plan.rds.id

  resources = [aws_db_instance.main.arn]
}

# ---------------------------------------------------------------------------
# CloudWatch alarms for RDS (SRE concerns #1, #2, #3)
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "rds_cpu" {
  alarm_name          = "${local.name_prefix}-rds-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS CPU > 80% for 3 consecutive minutes"
  alarm_actions       = [var.sns_alerts_arn]
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.id }
}

resource "aws_cloudwatch_metric_alarm" "rds_connections" {
  alarm_name          = "${local.name_prefix}-rds-connections-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS connection pool near exhaustion — risk of connection refused errors"
  alarm_actions       = [var.sns_alerts_arn]
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.id }
}

resource "aws_cloudwatch_metric_alarm" "rds_replica_lag" {
  alarm_name          = "${local.name_prefix}-replica-lag"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ReplicaLag"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Average"
  threshold           = 30
  alarm_description   = "Read replica lag > 30s — Risk VaR report at 06:00 may read stale data"
  alarm_actions       = [var.sns_alerts_arn]
  dimensions          = { DBInstanceIdentifier = aws_db_instance.replica.id }
}

resource "aws_cloudwatch_metric_alarm" "rds_free_storage" {
  alarm_name          = "${local.name_prefix}-rds-storage-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 10737418240   # 10 GB in bytes
  alarm_description   = "RDS free storage < 10 GB — storage auto-scaling may not keep up"
  alarm_actions       = [var.sns_alerts_arn]
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.id }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
output "rds_endpoint"           { value = aws_db_instance.main.endpoint }
output "rds_replica_endpoint"   { value = aws_db_instance.replica.endpoint }
output "redis_primary_endpoint" { value = aws_elasticache_replication_group.redis.primary_endpoint_address }
output "backup_vault_arn"       { value = aws_backup_vault.main.arn }

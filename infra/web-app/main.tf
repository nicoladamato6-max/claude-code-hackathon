terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  # Remote state (ADR-10 + discovery §10): prevents concurrent apply corruption
  backend "s3" {
    bucket         = "contoso-tfstate"
    key            = "web-app/terraform.tfstate"
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
      Workload   = "web-app"
      ManagedBy  = "terraform"
      CostCenter = "IT-Infrastructure"
    }
  }
}

locals {
  name_prefix = "contoso-web-${var.environment}"
}

# ---------------------------------------------------------------------------
# ECS Cluster
# ---------------------------------------------------------------------------
resource "aws_ecs_cluster" "web" {
  name = local.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"   # CloudWatch Container Insights (ADR-09)
  }
}

# ---------------------------------------------------------------------------
# IAM — task execution role (pulls image from ECR, reads Secrets Manager)
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals { type = "Service"; identifiers = ["ecs-tasks.amazonaws.com"] }
  }
}

resource "aws_iam_role" "exec" {
  name               = "${local.name_prefix}-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "exec_managed" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.db_secret_arn, var.redis_secret_arn, var.flask_secret_arn]
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "secrets-access"
  role   = aws_iam_role.exec.id
  policy = data.aws_iam_policy_document.secrets.json
}

# Task role — S3 presign (ADR-07 asset endpoint)
resource "aws_iam_role" "task" {
  name               = "${local.name_prefix}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "s3_assets" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::${var.s3_bucket_assets}/*"]
  }
}

resource "aws_iam_role_policy" "s3_assets" {
  name   = "s3-assets-read"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.s3_assets.json
}

# ---------------------------------------------------------------------------
# Security groups
# ---------------------------------------------------------------------------
resource "aws_security_group" "alb" {
  name   = "${local.name_prefix}-alb"
  vpc_id = var.vpc_id

  ingress { from_port = 443; to_port = 443; protocol = "tcp"; cidr_blocks = ["0.0.0.0/0"] }
  egress  { from_port = 0;   to_port = 0;   protocol = "-1";  cidr_blocks = ["0.0.0.0/0"] }
}

resource "aws_security_group" "app" {
  name   = "${local.name_prefix}-app"
  vpc_id = var.vpc_id

  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress { from_port = 0; to_port = 0; protocol = "-1"; cidr_blocks = ["0.0.0.0/0"] }
}

# ---------------------------------------------------------------------------
# ALB + HTTPS listener (ADR-07: ALB over NLB; ACM over self-signed)
# ---------------------------------------------------------------------------
resource "aws_lb" "web" {
  name               = local.name_prefix
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = var.public_subnets
}

resource "aws_lb_target_group" "web" {
  name        = local.name_prefix
  port        = 8080
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
    timeout             = 5
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# ---------------------------------------------------------------------------
# ECS Task Definition
# ---------------------------------------------------------------------------
resource "aws_ecs_task_definition" "web" {
  family                   = local.name_prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "web"
    image     = var.app_image
    essential = true

    portMappings = [{ containerPort = 8080, protocol = "tcp" }]

    # Secrets injected from Secrets Manager — never in plaintext (ADR-08)
    secrets = [
      { name = "DATABASE_URL", valueFrom = var.db_secret_arn    },
      { name = "REDIS_URL",    valueFrom = var.redis_secret_arn  },
      { name = "SECRET_KEY",   valueFrom = var.flask_secret_arn  },
    ]

    environment = [
      { name = "S3_BUCKET_ASSETS",       value = var.s3_bucket_assets },
      { name = "AWS_REGION",             value = var.aws_region        },
      { name = "SESSION_COOKIE_SECURE",  value = "true"                },
      { name = "FLASK_DEBUG",            value = "false"               },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = "/ecs/${local.name_prefix}"
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "web"
      }
    }

    healthCheck = {
      command     = ["CMD-SHELL", "curl -sf http://localhost:8080/healthz || exit 1"]
      interval    = 15
      timeout     = 5
      retries     = 3
      startPeriod = 30
    }
  }])
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${local.name_prefix}"
  retention_in_days = 90   # GDPR: 90-day hot retention (ADR-09, SRE concern #8)
}

# ---------------------------------------------------------------------------
# ECS Service — min 2 tasks; rolling update (no downtime deploys)
# ---------------------------------------------------------------------------
resource "aws_ecs_service" "web" {
  name            = local.name_prefix
  cluster         = aws_ecs_cluster.web.id
  task_definition = aws_ecs_task_definition.web.arn
  desired_count   = var.min_tasks
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnets
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 8080
  }

  deployment_minimum_healthy_percent = 100   # zero-downtime rolling deploy
  deployment_maximum_percent         = 200

  depends_on = [aws_lb_listener.https]
}

# ---------------------------------------------------------------------------
# Auto Scaling — scale out on CPU > 60% (SRE concern #2)
# ---------------------------------------------------------------------------
resource "aws_appautoscaling_target" "web" {
  max_capacity       = var.max_tasks
  min_capacity       = var.min_tasks
  resource_id        = "service/${aws_ecs_cluster.web.name}/${aws_ecs_service.web.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${local.name_prefix}-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.web.resource_id
  scalable_dimension = aws_appautoscaling_target.web.scalable_dimension
  service_namespace  = aws_appautoscaling_target.web.service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = 60.0
  }
}

# ---------------------------------------------------------------------------
# HTTP → HTTPS redirect (discovery §5: self-signed cert replaced with ACM)
# ---------------------------------------------------------------------------
resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_security_group_rule" "alb_http_ingress" {
  type              = "ingress"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.alb.id
}

# ---------------------------------------------------------------------------
# WAF WebACL — financial services rule set (discovery §5, ADR-07)
# Managed rules cover OWASP Top 10, SQLi, known bad inputs, IP reputation.
# ---------------------------------------------------------------------------
resource "aws_wafv2_web_acl" "web" {
  name  = "${local.name_prefix}-waf"
  scope = "REGIONAL"

  default_action { allow {} }

  # Rule 1 — AWS IP reputation list (blocks known malicious IPs)
  rule {
    name     = "AWSManagedRulesAmazonIpReputationList"
    priority = 10
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "IpReputationList"
      sampled_requests_enabled   = true
    }
  }

  # Rule 2 — Common rule set (OWASP Top 10)
  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 20
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  # Rule 3 — SQL injection (critical for financial DB-backed app)
  rule {
    name     = "AWSManagedRulesSQLiRuleSet"
    priority = 30
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesSQLiRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "SQLiRuleSet"
      sampled_requests_enabled   = true
    }
  }

  # Rule 4 — Known bad inputs (Log4Shell, Spring4Shell, etc.)
  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 40
    override_action { none {} }
    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "KnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  # Rule 5 — Rate limiting: max 100 req/5min per IP (discovery §5: credential stuffing)
  rule {
    name     = "RateLimitPerIp"
    priority = 50
    action { block {} }
    statement {
      rate_based_statement {
        limit              = 100
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "RateLimit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_prefix}-waf"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "web" {
  resource_arn = aws_lb.web.arn
  web_acl_arn  = aws_wafv2_web_acl.web.arn
}

resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${local.name_prefix}"
  retention_in_days = 90
}

resource "aws_wafv2_web_acl_logging_configuration" "web" {
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]
  resource_arn            = aws_wafv2_web_acl.web.arn
}

# ---------------------------------------------------------------------------
# CloudWatch alarm — 5xx error rate > 1% triggers SNS alert
# ---------------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "error_rate" {
  alarm_name          = "${local.name_prefix}-5xx-rate"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "Web-app 5xx errors exceed 10/min — potential incident"
  alarm_actions       = [var.sns_alerts_arn]
  ok_actions          = [var.sns_alerts_arn]

  dimensions = {
    LoadBalancer = aws_lb.web.arn_suffix
  }
}

output "alb_dns_name"  { value = aws_lb.web.dns_name }
output "waf_acl_arn"   { value = aws_wafv2_web_acl.web.arn }

variable "aws_region"    { default = "eu-west-1" }
variable "environment"   { default = "production" }
variable "app_image"     { description = "ECR image URI for web-app (tag must match git SHA)" }
variable "vpc_id"        { description = "VPC where ECS and ALB are deployed" }
variable "public_subnets"  { type = list(string); description = "ALB subnets (public)" }
variable "private_subnets" { type = list(string); description = "ECS task subnets (private)" }
variable "certificate_arn" { description = "ACM certificate ARN for ALB HTTPS listener" }
variable "db_secret_arn"   { description = "Secrets Manager ARN for DATABASE_URL" }
variable "redis_secret_arn" { description = "Secrets Manager ARN for REDIS_URL" }
variable "flask_secret_arn" { description = "Secrets Manager ARN for SECRET_KEY" }
variable "s3_bucket_assets" { default = "web-assets" }
variable "task_cpu"      { default = 512  }
variable "task_memory"   { default = 1024 }
variable "min_tasks"        { default = 2    }
variable "max_tasks"        { default = 10   }
variable "sns_alerts_arn"   { description = "SNS topic ARN from infra/shared for CloudWatch alarm actions" }
variable "environment"      { default = "production" }

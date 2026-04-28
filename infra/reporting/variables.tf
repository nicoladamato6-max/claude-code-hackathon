variable "aws_region"         { default = "eu-west-1" }
variable "environment"        { default = "production" }
variable "vpc_id"             { description = "VPC for RDS and ElastiCache" }
variable "private_subnets"    { type = list(string) }
variable "db_instance_class"  { default = "db.t3.medium" }
variable "db_allocated_gb"    { default = 200 }
variable "db_name"            { default = "contoso" }
variable "db_secret_arn"      { description = "Secrets Manager ARN for POSTGRES_PASSWORD (auto-rotation enabled)" }
variable "redis_node_type"    { default = "cache.t3.micro" }
variable "allowed_app_sg_ids" { type = list(string); description = "Security group IDs allowed to connect to RDS and Redis" }
variable "sns_alerts_arn"    { description = "SNS topic ARN from infra/shared for RDS alarm actions" }

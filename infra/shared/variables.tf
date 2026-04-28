variable "aws_region"      { default = "eu-west-1" }
variable "environment"     { default = "production" }
variable "project"         { default = "contoso-financial" }
variable "owner"           { default = "cloud-migration-team" }
variable "cost_center"     { default = "IT-Infrastructure" }
variable "alert_email"     { description = "Email address for CloudWatch alarm notifications (SNS)" }
variable "monthly_budget_usd" {
  default     = 600
  description = "Monthly AWS spend alert threshold — ~10% above €350/month estimate (01-memo.md §Cost analysis)"
}

# Workload: Customer-Facing Web App

## What this is

A Python/Flask web application that serves Contoso Financial's retail customers.
Runs on-premise today behind an Nginx reverse proxy. Target: containerized, behind
an Application Load Balancer, auto-scaling on CPU.

## Migration target

| Component | On-prem | Cloud equivalent | Local sim |
|-----------|---------|-----------------|-----------|
| App server | bare-metal Python | ECS Fargate task | Docker container |
| Static assets | local disk | S3 + CloudFront | MinIO (`web-assets` bucket) |
| Session store | Redis on same host | ElastiCache (Redis) | `redis` service in compose |
| Database | Postgres 13 | RDS PostgreSQL 15 | `postgres` service in compose |

## Claude guidance for this workload

- **Dockerfile**: multi-stage build. Builder stage installs deps; runtime stage is
  `python:3.12-slim`. Final image must run as a non-root user (`appuser`).
- **Health check**: expose `GET /healthz` returning `{"status": "ok"}` — ECS uses it.
- **Config**: all config via environment variables. No hardcoded hostnames, ports, or credentials.
- **Session handling**: Redis connection string comes from `REDIS_URL` env var.
- **Static assets**: upload path resolves from `S3_ENDPOINT_URL` + `S3_BUCKET_ASSETS`
  so the same code works against MinIO locally and S3 in cloud.
- **Secrets**: never log the value of `DATABASE_URL`, `REDIS_URL`, or any `*_KEY` var.

## Known issues discovered in Discovery (Challenge 2)

- `config.py` contains a hardcoded IP `10.0.1.45` for the Redis host — replace with env var.
- Session cookie `secure` flag is off — must be enabled before go-live.
- Static file path assumes `/var/www/assets` mount — replace with S3 client.

## Ports

| Environment | Port |
|-------------|------|
| Local dev | 5000 |
| Container (internal) | 8080 |
| ALB listener | 443 |

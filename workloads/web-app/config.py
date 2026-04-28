import os


class Config:
    # Flask core — never default SECRET_KEY in production
    SECRET_KEY = os.environ["SECRET_KEY"]
    DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    TESTING = False

    # Session (Redis-backed)
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 3600  # 1 hour

    # Database
    DATABASE_URL = os.environ["DATABASE_URL"]

    # Redis
    REDIS_URL = os.environ["REDIS_URL"]

    # S3 / MinIO
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL")  # None in AWS, set for MinIO locally
    S3_BUCKET_ASSETS = os.environ.get("S3_BUCKET_ASSETS", "web-assets")
    AWS_REGION = os.environ.get("AWS_REGION", "eu-west-1")

    # Observability
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

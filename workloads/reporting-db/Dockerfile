FROM postgres:15

# Install pgaudit and pg_cron — not shipped in the standard postgres:15 image.
# These are required by V1__init_schema.sql and verified by smoke tests.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       postgresql-15-pgaudit \
       postgresql-15-pg-cron \
    && rm -rf /var/lib/apt/lists/*

# Stage 1: install dependencies into an isolated prefix so the runtime image stays small
FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: lean runtime — no build tools, no pip cache
FROM python:3.12-slim
WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

# Non-root user required by CLAUDE.md and ECS security best-practice
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8080

# 2 workers handles the expected 30 concurrent users; adjust via env in ECS task definition
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "30", \
     "--access-logfile", "-", "--error-logfile", "-", "app:app"]

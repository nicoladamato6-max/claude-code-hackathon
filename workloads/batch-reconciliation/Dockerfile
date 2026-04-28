FROM python:3.12-slim AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
WORKDIR /app

COPY --from=builder /install /usr/local
COPY reconcile.py .

RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# AWS Batch passes JOB_DATE via environment variable — no ENTRYPOINT override needed
CMD ["python", "reconcile.py"]

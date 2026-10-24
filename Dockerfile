# ARM64-friendly image (also builds on amd64).
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    UV_SYSTEM_PYTHON=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

COPY pyproject.toml README.md ./
COPY app ./app

RUN uv pip install --system .

RUN mkdir -p /app/data

EXPOSE 8321

# Bind all interfaces inside the container; Compose publishes only on Tailscale IP.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8321"]

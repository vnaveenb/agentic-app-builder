FROM python:3.12-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime
WORKDIR /app

# Install Node.js 20 LTS (for Node/React/Angular sandbox execution)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY src/ ./src/
COPY shared/ ./shared/

RUN useradd -m -u 1001 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

# Pre-warm the npm cache with the pinned front-end build toolchain so generated
# React/Vite previews install fast (and resiliently) at runtime. Runs as appuser
# so the cache lands in /home/appuser/.npm; node_modules is discarded.
RUN mkdir -p /tmp/warm && cd /tmp/warm && \
    printf '%s' '{"name":"warm","version":"1.0.0","dependencies":{"react":"18.3.1","react-dom":"18.3.1"},"devDependencies":{"vite":"5.4.10","@vitejs/plugin-react":"4.3.4"}}' > package.json && \
    npm install --no-audit --no-fund && \
    rm -rf /tmp/warm

EXPOSE 8007

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8007/health || exit 1

# Init DB tables on startup, then run the app
CMD ["sh", "-c", "python -c 'import asyncio; from src.dev_agent.db.database import init_db; asyncio.run(init_db())' && uvicorn src.dev_agent.main:app --host 0.0.0.0 --port 8007"]

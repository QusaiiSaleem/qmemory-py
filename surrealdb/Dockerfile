# SurrealDB service for Qmemory — Railway deployment
# Custom Debian image (NOT official surrealdb image — it runs as non-root
# which causes permission errors with Railway volumes)

FROM debian:bookworm-slim

# Install SurrealDB with pinned version
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && curl -sSf https://install.surrealdb.com | sh -s -- --version v3.0.0 \
    && rm -rf /var/lib/apt/lists/*

# Create data directory for RocksDB persistence (Railway volume mounts here)
RUN mkdir -p /data

EXPOSE 8000

# Start with RocksDB engine, reading credentials from Railway env vars
# PORT: Railway injects this — must be 8000 for SurrealDB
# SURREAL_PASS: Set in Railway dashboard (never hardcode!)
CMD ["sh", "-c", "surreal start --bind 0.0.0.0:${PORT:-8000} --user ${SURREAL_USER:-root} --pass \"${SURREAL_PASS:-root}\" --log ${SURREAL_LOG:-info} rocksdb:/data/qmemory.db"]

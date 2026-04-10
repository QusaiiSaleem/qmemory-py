FROM python:3.11-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Copy everything then install
COPY . .
# Force rebuild: 2026-04-01-books-v1
RUN pip install --no-cache-dir .

EXPOSE 8080

# Default: run the API server
CMD ["uvicorn", "qmemory.app.main:api", "--host", "0.0.0.0", "--port", "8080"]

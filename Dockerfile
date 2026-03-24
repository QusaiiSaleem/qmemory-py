FROM python:3.11-slim AS base

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# Copy source
COPY . .

# Install the package itself
RUN pip install --no-cache-dir -e .

EXPOSE 8080

# Default: run the API server
CMD ["uvicorn", "qmemory.app.main:api", "--host", "0.0.0.0", "--port", "8080"]

FROM python:3.11-slim

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install
COPY pyproject.toml .
COPY core/ core/
COPY mcp_server/ mcp_server/
COPY mcp_proxy/ mcp_proxy/

RUN pip install --no-cache-dir -e .

# Non-root user
RUN useradd -m -u 1000 mcpuser
USER mcpuser

EXPOSE 8080 8000

ENV PYTHONUNBUFFERED=1

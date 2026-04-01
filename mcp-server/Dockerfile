# LifeRadar MCP Server — Python stdio MCP server
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

COPY mcp-server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcp-server/ ./mcp-server/

# Production runtime
FROM python:3.12-slim-bookworm

WORKDIR /app

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app/mcp-server/ ./mcp-server/

ENV PYTHONUNBUFFERED=1
ENV LIFE_RADAR_API_URL=http://host.docker.internal:8000

# Run via stdio — MCP servers use stdio transport
CMD ["python", "mcp-server/server.py"]

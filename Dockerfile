FROM python:3.12-slim

WORKDIR /app

# Install the package itself (runtime pins come from requirements.txt via pyproject.toml).
COPY pyproject.toml requirements.txt README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 app
USER app

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8002
# The tester dashboard is for private local use; opt in explicitly if you really want it.
ENV MCP_DASHBOARD_ENABLED=false

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health/live', timeout=2)"

# Behind a reverse proxy, set FORWARDED_ALLOW_IPS to the proxy address so uvicorn trusts
# X-Forwarded-For and the per-client rate limiter sees real client IPs.
CMD ["shortsmaker-mcp"]

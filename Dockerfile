FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV MAGNIT_MCP_HOST=0.0.0.0 \
    MAGNIT_MCP_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# Публичный запуск: Bearer-токен задаётся через MAGNIT_MCP_TOKEN в рантайме.
CMD ["magnit-mcp", "--transport", "http"]

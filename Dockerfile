FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md main.py config.yaml.example ./
COPY llmao/ ./llmao/
RUN uv sync --frozen --no-dev --no-editable \
    && cp config.yaml.example config.yaml
COPY litellm/ ./litellm/
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8443
# Mount or replace /app/config.yaml with real secrets in production.
# Leave server.certfile/keyfile blank for plain HTTP behind a TLS proxy.
CMD ["python", "main.py"]

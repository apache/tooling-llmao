FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY llmao/ ./llmao/
RUN uv sync --frozen --no-dev --no-editable
COPY litellm/ ./litellm/
ENV PATH="/app/.venv/bin:$PATH"
ENV LLMAO_HOST=0.0.0.0 LLMAO_PORT=8080
EXPOSE 8080
# Production note: front this with hypercorn and set LLMAO_AUTH_MODE=asf,
# LLMAO_LITELLM_MODE=proxy. For the simplest run, the built-in server is fine.
CMD ["python", "-m", "llmao.app"]

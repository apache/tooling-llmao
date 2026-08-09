FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md main.py ./
COPY config.yaml.example litellm.yaml.example model_list.yaml.example ./
COPY pages.py api.py ./
COPY llmao/ ./llmao/
COPY templates/ ./templates/
COPY static/ ./static/
RUN uv sync --frozen --no-dev --no-editable \
    && cp config.yaml.example config.yaml \
    && cp litellm.yaml.example litellm.yaml \
    && cp model_list.yaml.example model_list.yaml
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8443
# Mount real config.yaml, litellm.yaml, model_list.yaml in production (eyaml secrets).
# Hypercorn: python -m hypercorn main:llmao_app
CMD ["python", "main.py"]

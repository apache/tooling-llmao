# llmao — dev workflow. Uses UV (https://docs.astral.sh/uv/) to manage a local
# .venv so it works on PEP 668 "externally managed" systems without touching
# the system Python. Requires `uv` on PATH.

.PHONY: install run test config proxy build clean

install:
	uv sync

run: install
	uv run python -m llmao.app

test: install
	uv run pytest tests/ -q

# Regenerate the litellm proxy config from the catalog.
config: install
	uv run python scripts/render_litellm_config.py > litellm/config.yaml

# Run the real litellm proxy (production backend).
proxy: install
	uv run litellm --config litellm/config.yaml

# Build the production Docker image (used by Puppet: `make build`).
# Regenerates the litellm config from the catalog first so the image ships
# a config.yaml that matches the catalog.
build: config
	docker build -t llmao:latest .

clean:
	rm -f llmao-state.json demo-state.json *.tmp
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .venv

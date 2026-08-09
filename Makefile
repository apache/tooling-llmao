# llmao — dev workflow. Uses UV (https://docs.astral.sh/uv/) to manage a local
# .venv so it works on PEP 668 "externally managed" systems without touching
# the system Python. Requires `uv` on PATH.
#
# Local run needs config.yaml (from config.yaml.example) and, for OAuth,
# TLS certs under certs/ — see certs/README.md.

.PHONY: install run test proxy db build clean thirdparty

install:
	uv sync

# System PostgreSQL: create llmao/litellm role+db, prisma generate + db push.
db: install
	uv run python bin/setup_litellm_db.py

# Vendor Bootstrap + icons into static/ (see bin/fetch-thirdparty.sh).
thirdparty:
	./bin/fetch-thirdparty.sh

run: install
	uv run python main.py

test: install
	uv run pytest tests/ -q

# Run the LiteLLM proxy. Requires litellm.yaml + model_list.yaml (from *.example).
proxy: install
	@test -f litellm.yaml || (echo "Missing litellm.yaml — copy litellm.yaml.example" >&2; exit 1)
	@test -f model_list.yaml || (echo "Missing model_list.yaml — copy model_list.yaml.example" >&2; exit 1)
	uv run litellm --config litellm.yaml

# Build the production Docker image (optional; systemd is the preferred deploy).
build:
	docker build -t llmao:latest .

clean:
	rm -f llmao-state.json demo-state.json *.tmp
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -rf .venv

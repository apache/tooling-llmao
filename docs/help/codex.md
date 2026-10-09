# Codex

Codex uses the Responses API under `/v1`. Put this in `$CODEX_HOME/config.toml` (by default `~/.codex/config.toml`). `$LLMAO_KEY` is the personal access token from [My Keys](/keys). The model id comes from the [Models page](/models). This example uses `qwen3.8-27b`.

```toml
model_provider = "llmao"
model = "qwen3.8-27b"

[model_providers.llmao]
name = "llmao"
base_url = "https://llm.apache.org/v1"
env_key = "LLMAO_KEY"
wire_api = "responses"
```

```sh
export LLMAO_KEY=<your personal access token>
codex
```

`codex -m qwen3.8-27b` selects the model for one run. `wire_api` stays `responses`.

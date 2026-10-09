# Pointing a harness at llm.apache.org by hand

What `amem launch --auth gateway` sets for each harness, rewritten for a harness run on
its own against the Foundation's gateway, with no egress in between. Taken from the
`setup_*` adapters in `crates/sandbox/src/lib.rs`, `route_for` and `apply_run_options` in
`crates/memory/src/main.rs`, and the gateway table in [README.md](README.md).

Under the launcher, a harness's base URL is `<relay>/llmao/<rest>` and the egress sends
it to `https://llm.apache.org/<rest>` with `Authorization: Bearer <PAT>`. By hand, the
harness talks to the gateway itself, so:

- the base URL is `https://llm.apache.org` plus the same `<rest>` (`/v1` or nothing, per
  harness below);
- the placeholder key the launcher gives the harness (`egress`) is your llmao PAT;
- the memory server's MCP entry, the hooks and the auto-update switches the launcher
  writes are left out; they belong to the launcher, not to the gateway.

Below, `$LLMAO_KEY` is the PAT and `$MODEL` one of the gateway's models. On 5 October
those were `qwen3.8-27b`, `gemma4-26b` and `qwen3-8b`; the current list is
`curl -H "Authorization: Bearer $LLMAO_KEY" https://llm.apache.org/v1/models`.

## What the egress does that a manual setup does not

The gateway route is not a plain forward. Four things happen in the egress
(`crates/egress/src/lib.rs`, `Destination`), and a harness that relied on one of them
behaves differently by hand:

| Rewrite | Affects | Without it |
|---|---|---|
| a `system`-role message in `messages` is folded into `system` | Claude Code | the Qwen chat template refuses the request: `System message must be at the beginning` |
| a `functionResponse` in a `model` turn is moved to a `user` turn | Antigravity, Gemini CLI | LiteLLM's Gemini adapter drops the tool result and the model answers without it |
| output tokens asked for are capped at 32768 | Muse Code | Muse asks for 128k, which a 131k-context model refuses once the prompt is counted |
| `GET /muse-code/models` at the origin is answered from `/v1/models` | Muse Code | the gateway has no such path, and Muse's first call fails |

The credential also differs: the egress always sends the PAT as a bearer token and strips
`x-api-key` and `x-goog-api-key`. Where a harness sends its key some other way, that is
noted below as untried.

## Claude Code

Anthropic Messages at the root: no `/v1` in the base.

```sh
export ANTHROPIC_BASE_URL=https://llm.apache.org
export ANTHROPIC_AUTH_TOKEN="$LLMAO_KEY"     # sent as a bearer; not ANTHROPIC_API_KEY, which is x-api-key
export CLAUDE_CODE_EFFORT_LEVEL=medium       # default `high` is Anthropic-only; vLLM takes low, medium, xhigh
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=120000 # Claude Code assumes 200k and compacts too late
# one model for every role, since the gateway answers only for the models it serves
for v in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
         ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_SMALL_FAST_MODEL CLAUDE_CODE_SUBAGENT_MODEL; do
  export "$v=$MODEL"
done
claude
```

Caveat: the system-message fold above. It was found on Qwen; llmao's own notes recommend
`gemma4-26b` for Claude Code, since a model that reasons before it emits can make Claude
Code abandon the stream.

## Codex

Responses API under `/v1`. In `$CODEX_HOME/config.toml` (default `~/.codex/config.toml`):

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
codex                  # or: codex -m "$MODEL"
```

## Qwen Code

Chat completions under `/v1`. In `~/.qwen/settings.json`:

```json
{ "security": { "auth": { "selectedType": "openai" } } }
```

```sh
export OPENAI_BASE_URL=https://llm.apache.org/v1
export OPENAI_API_KEY="$LLMAO_KEY"
qwen -m "$MODEL"
```

## OpenCode

Chat completions under `/v1`, as an OpenAI-compatible provider named `llmao`. The model has
to be declared, since models.dev does not list the gateway's. In
`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "llmao": {
      "npm": "@ai-sdk/openai-compatible",
      "options": { "baseURL": "https://llm.apache.org/v1", "apiKey": "{env:LLMAO_KEY}" },
      "models": { "qwen3.8-27b": { "name": "qwen3.8-27b" } }
    }
  }
}
```

```sh
opencode -m "llmao/$MODEL"
```

The launcher writes the key literally; `{env:LLMAO_KEY}` is OpenCode's own substitution and
keeps the PAT out of the file.

## Grok Build

Grok checks a model id against its catalog, so the gateway's model is a `[model.<id>]`
block on chat completions. In `~/.grok/config.toml`:

```toml
[model."qwen3.8-27b"]
name = "qwen3.8-27b"
model = "qwen3.8-27b"
base_url = "https://llm.apache.org/v1"
api_key = "<your PAT>"
api_backend = "chat_completions"
context_window = 131072
```

```sh
export GROK_XAI_API_BASE_URL=https://llm.apache.org/v1
export XAI_API_KEY="$LLMAO_KEY"
grok -m "$MODEL"
```

Only API-key inference reads `GROK_XAI_API_BASE_URL`; a stored `~/.grok/auth.json` from a
`grok login` outranks the key and sends calls to Grok's own proxy, so move it aside.

## pi

A provider of its own in `~/.pi/agent/models.json`, on chat completions, with the model
listed; pi offers only the models its providers list, so a run names one:

```json
{
  "providers": {
    "llmao": {
      "baseUrl": "https://llm.apache.org/v1",
      "api": "openai-completions",
      "apiKey": "<your PAT>",
      "models": [{ "id": "qwen3.8-27b", "name": "qwen3.8-27b" }]
    }
  }
}
```

```sh
pi --model "llmao/$MODEL"
```

## Gemini CLI

The Gemini shape at the root, translated by the gateway. In `~/.gemini/settings.json`:

```json
{ "security": { "auth": { "selectedType": "gemini-api-key" } } }
```

```sh
export GOOGLE_GEMINI_BASE_URL=https://llm.apache.org
export GEMINI_API_KEY="$LLMAO_KEY"
gemini -m "$MODEL"
```

Untried by hand: Gemini CLI sends the key as `x-goog-api-key`, not a bearer. And the
launcher applies the `functionResponse` fold on this route; that the proven run needed it
for Gemini CLI, as it does for Antigravity, is not established.

## Antigravity CLI (`agy`)

The Gemini shape at the root. In `~/.gemini/antigravity-cli/settings.json`:

```json
{ "modelProvider": "gemini" }
```

```sh
export GOOGLE_GEMINI_BASE_URL=https://llm.apache.org
export GEMINI_API_KEY="$LLMAO_KEY"
agy --model "gemini-api://$MODEL"
```

A model outside Google's catalog is named `gemini-api://<model>`, which agy sends as
`v1beta/models/<model>`. Caveats: the key travels as `x-goog-api-key`, untried by hand;
and without the `functionResponse` fold a tool's result is dropped, so expect a one-shot
answer to work and a tool call not to.

## Muse Code (`muse`)

```sh
export META_API_KEY="$LLMAO_KEY"
muse exec "<prompt>" --base-url https://llm.apache.org/v1 --reasoning-effort medium --model "$MODEL"
```

`--base-url` is taken only by `exec` and the TUI, after the prompt. `--reasoning-effort
medium` because Muse asks for `high`, which the gateway's models refuse. Not workable by
hand as it stands: Muse's first call is `GET https://llm.apache.org/muse-code/models`,
which the gateway does not serve, and it asks for 128k output tokens. Both are what the
egress rewrites; without it Muse needs a shim that does the same.

## Copilot CLI

Not possible: Copilot CLI has no base-URL override and talks only to GitHub.

## Summary

| Harness | Base URL | Key goes in | Model |
|---|---|---|---|
| Claude Code | `https://llm.apache.org` | `ANTHROPIC_AUTH_TOKEN` | `ANTHROPIC_MODEL` and the role defaults |
| Codex | `https://llm.apache.org/v1`, `wire_api = "responses"` | `env_key` of the provider | `model` / `-m` |
| Qwen Code | `https://llm.apache.org/v1` | `OPENAI_API_KEY` | `-m` |
| OpenCode | `https://llm.apache.org/v1`, `@ai-sdk/openai-compatible` | `options.apiKey` | `llmao/<model>`, declared |
| Grok Build | `https://llm.apache.org/v1`, `chat_completions` | `api_key` / `XAI_API_KEY` | `[model.<id>]`, `-m <id>` |
| pi | `https://llm.apache.org/v1`, `openai-completions` | `apiKey` in `models.json` | `llmao/<model>`, listed |
| Gemini CLI | `https://llm.apache.org` | `GEMINI_API_KEY` | `-m` |
| Antigravity | `https://llm.apache.org` | `GEMINI_API_KEY` | `gemini-api://<model>` |
| Muse Code | `https://llm.apache.org/v1` | `META_API_KEY` | needs the egress |
| Copilot CLI | none | none | not possible |

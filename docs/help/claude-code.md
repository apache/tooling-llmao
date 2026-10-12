# Claude Code

Point Claude Code at `https://llm.apache.org`. Use a model id from the [Models page](/models). This example uses `gemma4-26b`, an open-weight model the gateway hosts. `$LLMAO_KEY` is the personal access token from [My Keys](/keys).

```sh
export ANTHROPIC_BASE_URL=https://llm.apache.org
export ANTHROPIC_AUTH_TOKEN="$LLMAO_KEY"
export CLAUDE_CODE_EFFORT_LEVEL=medium
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=120000
export CLAUDE_CODE_MAX_OUTPUT_TOKENS=8192
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export MODEL=gemma4-26b
for v in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
         ANTHROPIC_DEFAULT_HAIKU_MODEL CLAUDE_CODE_SUBAGENT_MODEL; do
  export "$v=$MODEL"
done
claude
```

Set `ANTHROPIC_AUTH_TOKEN`, not `ANTHROPIC_API_KEY`. The base URL has no `/v1`. Every role uses the same model, because the gateway answers only for the models it serves.

Once it starts, `/status` shows the address and the credentials the session is using. Check it if you have signed in to Claude Code before: the address should be `https://llm.apache.org`.

`CLAUDE_CODE_MAX_CONTEXT_TOKENS` matters. Claude Code assumes a 200k window for a model it does not recognise and compacts too late. Set it below the model's window. The [Models page](/models) lists each window.

`CLAUDE_CODE_MAX_OUTPUT_TOKENS` goes with it. Prompt and response share the window, and a request that asks for more than is left is rejected before it runs. The two settings must add up to less than the window: 120000 and 8192 against 131072.

Prefer `gemma4-26b`. A model that reasons before it emits can sit silent long enough that Claude Code abandons the stream and retries. `gemma4-26b` starts emitting immediately.

`CLAUDE_CODE_EFFORT_LEVEL=medium` is for models that refuse the default `high`. `qwen3.8-27b` accepts `low`, `medium`, and `xhigh`. `gemma4-26b` accepts the default, so the effort line is optional there. `claude --effort medium` sets the same value for one run.

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` turns off update checks, telemetry, and error reports, which do not go through the gateway.

## Keeping the settings

To avoid exporting these in every shell, put them in the `env` block of `~/.claude/settings.json`. Claude Code applies them each time it starts, and other tools in your shell do not see them.

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://llm.apache.org",
    "ANTHROPIC_AUTH_TOKEN": "<your personal access token>",
    "ANTHROPIC_MODEL": "gemma4-26b",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "gemma4-26b",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gemma4-26b",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gemma4-26b",
    "CLAUDE_CODE_SUBAGENT_MODEL": "gemma4-26b",
    "CLAUDE_CODE_EFFORT_LEVEL": "medium",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "120000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

That file holds your token, so keep it in your home directory and out of any repository.

## What does not work

Tools such as web search fail in Claude Code today. Conversation works.

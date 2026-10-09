# Claude Code

Point Claude Code at `https://llm.apache.org`. Use a model id from the [Models page](/models). This example uses `gemma4-26b`, an open-weight model the gateway hosts. `$LLMAO_KEY` is the personal access token from [My Keys](/keys).

```sh
export ANTHROPIC_BASE_URL=https://llm.apache.org
export ANTHROPIC_AUTH_TOKEN="$LLMAO_KEY"
export CLAUDE_CODE_EFFORT_LEVEL=medium
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=120000
export MODEL=gemma4-26b
for v in ANTHROPIC_MODEL ANTHROPIC_DEFAULT_OPUS_MODEL ANTHROPIC_DEFAULT_SONNET_MODEL \
         ANTHROPIC_DEFAULT_HAIKU_MODEL ANTHROPIC_SMALL_FAST_MODEL CLAUDE_CODE_SUBAGENT_MODEL; do
  export "$v=$MODEL"
done
claude
```

Set `ANTHROPIC_AUTH_TOKEN`, not `ANTHROPIC_API_KEY`. The base URL has no `/v1`. Every role uses the same model, because the gateway answers only for the models it serves.

`CLAUDE_CODE_MAX_CONTEXT_TOKENS` matters. Claude Code assumes a 200k window for a model it does not recognise and compacts too late. Set it below the model's window. 120000 against 131072 leaves room for the response. The [Models page](/models) lists each window.

Prefer `gemma4-26b`. A model that reasons before it emits can sit silent long enough that Claude Code abandons the stream and retries. `gemma4-26b` starts emitting immediately.

`CLAUDE_CODE_EFFORT_LEVEL=medium` is for models that refuse the default `high`. `qwen3.8-27b` accepts `low`, `medium`, and `xhigh`. `gemma4-26b` accepts the default, so the effort line is optional there. `claude --effort medium` sets the same value for one run.

Tools such as web search fail in Claude Code today. Conversation works.

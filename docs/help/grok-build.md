# Grok Build

Grok Build checks a model id against its own catalog, so the gateway's
model is a `[model.<id>]` block on chat completions. Put this in
`~/.grok/config.toml`. The model id comes from the [Models
page](/models). This example uses `qwen3.8-27b`. The key is the
personal access token from [My Keys](/keys).

```toml
[model."qwen3.8-27b"]
name = "qwen3.8-27b"
model = "qwen3.8-27b"
base_url = "https://llm.apache.org/v1"
api_key = "<your personal access token>"
api_backend = "chat_completions"
context_window = 131072
```

Use the `/model` command within Grok Build, or start it with your
selected model:

```sh
grok --model qwen3.8-27b  # or -m
```

The TOML block carries the address and the key, so no environment
variables are needed.

## Multiple Models and Keys

Each TOML block specifies a single model and the key to provide to
LLMAO to use that model. If you want to access multiple models, then
you'll want to multiple blocks. Something like:

```toml
[model."llmao-httpd-qwen3.8"]
...
[model."llmao-rai-gemma4"]
...
```

## Potential Issues

* If you're having problems authenticating with LLMAO and your key, then look for
  `~/.grok/auth.json` left by `grok login`. It might be interfering, so set that aside
  and pull back when you return to the default Grok model(s) served by xAI.

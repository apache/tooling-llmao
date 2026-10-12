# Grok Build

Grok Build checks a model id against its own catalog, so the gateway's model is a `[model.<id>]` block on chat completions. Put this in `~/.grok/config.toml`. The model id comes from the [Models page](/models). This example uses `qwen3.8-27b`. The key is the personal access token from [My Keys](/keys).

```toml
[model."qwen3.8-27b"]
name = "qwen3.8-27b"
model = "qwen3.8-27b"
base_url = "https://llm.apache.org/v1"
api_key = "<your personal access token>"
api_backend = "chat_completions"
context_window = 131072
```

```sh
grok -m qwen3.8-27b
```

The block carries the address and the key, so no environment variables are needed.

A `~/.grok/auth.json` left by `grok login` outranks the key and sends calls elsewhere. Move that file aside.

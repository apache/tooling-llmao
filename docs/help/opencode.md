# OpenCode

This is written against OpenCode 2.x, which changed the shape of the provider
config. The gateway is not in OpenCode's model catalog, so the provider and its
models are declared by hand.

The key does not go in the config file. OpenCode keeps credentials in its own
store, and `/connect` is how they get there. Setting the key in the config — as
a literal, as `{env:LLMAO_KEY}`, or by naming the variable under `env` — does
not work: the provider loads, the models appear, and then every request comes
back `Authentication Error, No api key passed in.` from the gateway, because
nothing was attached to it.

## Install

See [opencode.ai](https://opencode.ai) for the CLI and the desktop build. Both
read the same config file and share the same credential store.

## `~/.config/opencode/opencode.json`

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "llmao": {
      "name": "Apache LLMAO",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": { "baseURL": "https://llm.apache.org/v1" },
      "models": {
        "gemma4-26b": {
          "name": "Gemma 4 26B-A4B",
          "limit": { "context": 131072, "output": 32768 },
          "capabilities": { "tools": true, "input": ["text", "image"] }
        },
        "qwen3.8-27b": {
          "name": "Qwen3.8 27B",
          "limit": { "context": 131072, "output": 32768 },
          "capabilities": { "input": ["text", "image"] }
        },
        "qwen3-8b": {
          "name": "Qwen3 8B",
          "limit": { "context": 40960, "output": 32768 },
          "capabilities": { "input": ["text"] }
        }
      }
    }
  },
  "model": "llmao/gemma4-26b"
}
```

`settings.baseURL` needs the `/v1` suffix. No credential appears anywhere in
this file.

## The key

Create a personal access token on [My Keys](/keys). Then, in OpenCode, run:

```
/connect
```

Pick the provider and paste the token. The same flow works in the terminal
interface and in the desktop app.

This is the only way to give OpenCode the key, and it is also the way you would
want to. The token is stored once and persists, so there is no
`export LLMAO_KEY=…` to put the secret into shell history, nothing on a command
line for `ps` to show, and no environment variable whose value you later have to
reason about. A config file would have the same properties, but OpenCode will
not read the key from one — see above — so connecting is both the only option
and the safe one.

## More than one project key

A personal access token belongs to one project, and the credential store holds
one credential per provider entry. Two projects therefore mean two provider
entries, each connected separately. Name them so the picker is readable:

```json
"providers": {
  "llmao-netbeans": {
    "name": "Apache LLMAO - NetBeans",
    "package": "@opencode/ai/providers/openai-compatible",
    "settings": { "baseURL": "https://llm.apache.org/v1" },
    "models": { "gemma4-26b": { "name": "Gemma 4 26B-A4B" } }
  },
  "llmao-tooling": {
    "name": "Apache LLMAO - Tooling",
    "package": "@opencode/ai/providers/openai-compatible",
    "settings": { "baseURL": "https://llm.apache.org/v1" },
    "models": { "gemma4-26b": { "name": "Gemma 4 26B-A4B" } }
  }
}
```

Run `/connect` once for each. Models are then selected as
`llmao-netbeans/gemma4-26b`, so the project is visible in the model name.

## Which model

`gemma4-26b` is the default above because OpenCode is an interactive agent that
calls tools, and that is the model to use for that. The two Qwen models are
listed so you can select them for one-shot work; they are declared without a
tool-use claim, because that has not been checked through the gateway.

OpenCode cannot ask the gateway what it serves — model discovery works only for
a few local servers, not for a custom provider, so anything not in the `models`
map is invisible to it. The [Models page](/models) is the live list, and a model
that reads Unavailable there will fail whatever the config says. When the set
changes, this file is what has to change with it.

The `limit` values matter more than they look. OpenCode cannot detect them for a
declared model and falls back to its own defaults, so leaving them out lets it
send a prompt longer than the model's context, or ask for more output than the
model will give.

## Run

```shell
opencode
```

`/models` switches between the configured models.

## If the provider does not appear

`/models` lists a provider declared this way, and connecting is about the
credential rather than the listing. If it is missing from the picker anyway,
switching any other provider group off and back on in the providers UI forces
the list to rebuild — a display bug reported upstream as
[#50726](https://github.com/anomalyco/opencode/issues/50726).

# Qwen Code

This setup has been tested with Qwen Code 0.26.1 on macOS.

## Install

```shell
npm install -g @qwen-code/qwen-code@latest
```

(`brew install qwen-code` also works.)

## `~/.qwen/settings.json`

```json
{
  "modelProviders": {
    "openai": [
      {
        "id": "qwen3.8-27b",
        "name": "Apache LLMAO - Qwen3.8 27B",
        "baseUrl": "https://llm.apache.org/v1",
        "envKey": "LLMAO_KEY"
      },
      {
        "id": "gemma4-26b",
        "name": "Apache LLMAO - Gemma 4 26B",
        "baseUrl": "https://llm.apache.org/v1",
        "envKey": "LLMAO_KEY"
      },
      {
        "id": "qwen3-8b",
        "name": "Apache LLMAO - Qwen3 8B",
        "baseUrl": "https://llm.apache.org/v1",
        "envKey": "LLMAO_KEY"
      }
    ]
  },
  "security": {
    "auth": {
      "selectedType": "openai"
    }
  },
  "model": {
    "name": "qwen3.8-27b"
  }
}
```

Add one `modelProviders.openai` entry for each model ID on the [Models page](https://llm.apache.org/models). Note that `baseUrl` needs the `/v1` suffix.

## API key

Qwen Code loads `~/.qwen/.env` automatically, so the personal access token from [My Keys](https://llm.apache.org/keys) can go there:

```
LLMAO_KEY=<your personal access token>
```

(`chmod 600 ~/.qwen/.env`). Running `export LLMAO_KEY=...` in the shell works too, the same way as in the Codex guide.

## Run

```shell
qwen
```

Use `/model` inside Qwen Code to switch between the configured models.

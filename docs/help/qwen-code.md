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
        "envKey": "LLMAO_KEY_QWEN3_8_27B"
      },
      {
        "id": "gemma4-26b",
        "name": "Apache LLMAO - Gemma 4 26B",
        "baseUrl": "https://llm.apache.org/v1",
        "envKey": "LLMAO_KEY_GEMMA4_26B"
      },
      {
        "id": "qwen3-8b",
        "name": "Apache LLMAO - Qwen3 8B",
        "baseUrl": "https://llm.apache.org/v1",
        "envKey": "LLMAO_KEY_QWEN3_8B"
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

Each LLMAO personal access token is issued for one project and one model, so each entry names its own `envKey`. Qwen Code (0.26.1) ignores an `apiKey` field inside a `modelProviders` entry, so the tokens go in environment variables, not in `settings.json`.

## API keys

Create one personal access token per model on [My Keys](https://llm.apache.org/keys), for the project you are working on. Put them in a `.qwen/.env` file in that project's checkout, one line per `envKey`:

```
LLMAO_KEY_QWEN3_8_27B=<your qwen3.8-27b token for this project>
LLMAO_KEY_GEMMA4_26B=<your gemma4-26b token for this project>
LLMAO_KEY_QWEN3_8B=<your qwen3-8b token for this project>
```

Qwen Code looks for `.qwen/.env` in the directory you start it from and then in each parent directory, so a file at the top of the checkout covers the whole tree. If you keep all of a project's checkouts under one directory, one file there covers all of them. Do not commit the file: add `.qwen/.env` to `.git/info/exclude` (or `.gitignore`), and `chmod 600` it.

If you only work on one project, `~/.qwen/.env` works for every directory. Values in a project's `.qwen/.env` take precedence over `~/.qwen/.env`, one variable at a time. You only need tokens for the models you use; leave out the entries for the rest.

## Run

```shell
qwen
```

Use `/model` inside Qwen Code to switch between the configured models.

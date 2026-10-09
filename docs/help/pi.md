# Pi

This connects the Pi coding agent to `https://llm.apache.org/v1` with the [pi-localllm-provider](https://www.npmjs.com/package/pi-localllm-provider) extension. You need Pi installed (`pi --version`; see [pi.dev](https://pi.dev)) and a personal access token from My Keys.

Install the extension:

```text
pi install npm:pi-localllm-provider
```

Start Pi and open the extension:

```text
/localllm
```

Choose **Add server**.

1. Name it `Apache LLMAO`, or another name you prefer.
2. Base URL: `https://llm.apache.org/v1`
3. API key: your personal access token.

Confirm the server. Pi lists the models the gateway offers. Enable all of them, or a subset. They then sit alongside your other providers. The current set includes `gemma4-26b`, `qwen3-8b`, and `qwen3.8-27b`. The Models page is the list.

# FAQ

## Where do I get a key?

Sign in, open My Keys, and create a personal key. The model id on the Models page is the `model` parameter.

## How do I connect a tool?

Point a tool that speaks the OpenAI API at `https://llm.apache.org` and use your personal access token as the API key. Setup for each tool:

* [Claude Code](/help/claude-code)
* [Codex](/help/codex)
* [Grok Build](/help/grok-build)
* [Pi](/help/pi)

## Why did a request succeed with an empty answer?

A small output budget on a reasoning model can return HTTP 200 with empty content. The model spent the budget thinking. Where the tool allows it, turn reasoning off:

```json
"chat_template_kwargs": {"enable_thinking": false}
```

## Why was the request rejected before it ran?

Prompt and output share the context window. A client whose output budget is larger than that window is rejected before the prompt is counted. Raising the client's context setting makes that worse. The Models page lists each window.

## Why did the answer stop at a round number?

A stop on a round token count is the output budget. Raise it. A stop on a round amount of clock time is a timeout.

## Who can see my prompts?

The Models page states that for each model. Private means every deployment runs on infrastructure the ASF controls. External means every request is sent to a third-party provider. MIXED means a request may take either path, and you cannot choose which.

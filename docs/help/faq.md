# FAQ

## Where do I get a key?

[if-any uid]
Open [[]My Keys](/keys) and create a personal key.
[else]
[[]Sign in](/auth?login=/), open [[]My Keys](/keys), and create a personal key.
[end]
The model id on the [[]Models page](/models) is the `model` parameter.

A personal key belongs to you and to one project you are a member of. The secret is shown once, when you create the key. Copy it then.

## Can a script have its own key?

Yes. Ask for one under **Request a key** on [[]My Keys](/keys), and say what it is for. The same request covers work outside a project. If it is approved, you create the key there. The secret is shown once, to you, and passes through nobody else.

## How do I connect a tool?

Point a tool that speaks the OpenAI API at `https://llm.apache.org` and use your personal access token as the API key. Many tools need only two variables:

```sh
export OPENAI_BASE_URL=https://llm.apache.org/v1
export OPENAI_API_KEY=<your personal access token>
```

`GET https://llm.apache.org/v1/models` with that key lists the models it can reach.

Setup for each tool:

* [[]Claude Code](/help/claude-code)
* [[]Codex](/help/codex)
* [[]Grok Build](/help/grok-build)
* [[]Pi](/help/pi)

## Why did a request succeed with an empty answer?

A small output budget on a reasoning model can return HTTP 200 with empty content, a `finish_reason` of `length`, and no error. The model spent the budget thinking. Some models reason unless told not to, and others do the reverse. Where the tool allows it, turn reasoning off:

```json
"chat_template_kwargs": {"enable_thinking": false}
```

## Why was the request rejected before it ran?

Prompt and output share the context window. A client whose output budget is larger than that window is rejected before the prompt is counted. Raising the client's context setting makes that worse. The [[]Models page](/models) lists each window.

## Why did the answer stop at a round number?

A stop on a round token count is the output budget. Raise it. A stop on a round amount of clock time is a timeout.

## What does the Models page tell me?

For each model: the id to use, whether it is available now, its context window, its licence, where its weights and training data come from, and who can see your prompts.

## Who can see my prompts?

The [[]Models page](/models) states that for each model. Private means every deployment runs on infrastructure the ASF controls. External means every request is sent to a third-party provider. Mixed means a request may take either path, and you cannot choose which.

## Why are there two Qwen3 8B models?

`qwen3-8b` and `qwen3-8b-bf16` are the same model. They run on different hardware, and one of the machines cannot load the compressed weights the other uses. Answers should be equivalent. Use whichever the [[]Models page](/models) shows as available.

The same goes for `qwen3.8-27b` and `qwen3.8-27b-int4`: one model, with the second compressed further so it runs on smaller cards. Red Hat, who did the compression, measures it within a point or two of the original.

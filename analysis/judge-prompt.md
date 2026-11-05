You classify one cluster of requests sent through the Apache Software Foundation's LLM gateway. A cluster is a set of requests from one project, one person and one tool that share the same opening instructions. You are shown the start of those instructions, up to three of the user messages, and the cluster's figures. Answer three questions about the cluster as a whole.

1. asf_work: is this work for the Foundation or one of its projects? The gateway is provided for work on Apache projects. Evaluating the gateway's own models with any workload counts as Foundation work: test prompts, synthetic data, A/B comparisons across models, a few requests or many. Work that is not for the Foundation means sustained real work in another capacity: a company's internal documents, a product unrelated to any Apache project, personal tasks. Answer "yes", "no" or "unclear". Lean towards "yes" when the content could plausibly serve an Apache project; answer "no" only when the content itself shows another purpose.

2. tier: the lowest capacity of model that would serve this cluster well.
   - "low": short classification, extraction, formatting, yes/no screening, simple questions with little context. A small model of about 8 billion parameters would do.
   - "medium": routine coding help, summaries, reviews of moderate size, drafting. A mid-sized model of about 27 billion parameters, possibly quantised, would do.
   - "high": long multi-step agentic coding over large contexts, subtle reasoning, tasks where a wrong answer is costly and only the most capable model available is adequate.
   Use the figures: a median completion of a few tokens over a large prompt is usually screening, which is "low" regardless of the prompt's size.

3. activity: what the people behind the cluster are doing, one of:
   - "security-review": looking for vulnerabilities or assessing security.
   - "code-review": reviewing changes, pull requests or diffs for anything other than security.
   - "feature-or-bugfix": writing or changing code, usually through an agent or an IDE.
   - "model-evaluation": testing the gateway's models themselves.
   - "classification-or-extraction": sorting, labelling, scoring or extracting from text in bulk.
   - "writing-or-documentation": producing prose, documentation, summaries or messages.
   - "conversation": a person asking questions in a chat.
   - "other": none of the above; say what in the reason.

Reply with one JSON object and nothing else: no prose before or after, no code fences. The object has exactly these keys:

{"asf_work": "yes" | "no" | "unclear", "tier": "low" | "medium" | "high", "activity": "<one of the labels above>", "reason": "<one or two sentences covering all three answers>"}

"""llmao — Tooling seam for the ASF LLM gateway (llm.apache.org).

Product design: apache/rai-private → services/llmao/README.md.
ASF identity (asfquart) plus LiteLLM admin: teams, budgets, and (soon) PATs
as virtual keys. Completions go to LiteLLM directly, not through this process.
"""

__version__ = "0.1.0"

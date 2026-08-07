"""llmao — Tooling seam for the ASF LLM gateway (llm.apache.org).

Product design: apache/rai-private → services/llmao/README.md.
Always asfquart for Apache identity; LiteLLM admin for teams, budgets, and
(soon) PATs as virtual keys. Completions go to LiteLLM directly.
"""

__version__ = "0.1.0"

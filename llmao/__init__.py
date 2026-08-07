"""llmao — control plane for ASF LiteLLM access.

ASF identity (asfquart) plus remote LiteLLM admin: project teams, budgets, and
(soon) virtual keys. Completions are not proxied here — clients call the
LiteLLM proxy directly with keys this service will manage.
"""

__version__ = "0.1.0"

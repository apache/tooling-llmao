"""Minimal HTML for control-plane status and dev login.

No chat UI. A later phase may grow a key/budget admin console here; for now
this is only enough to sign in (dev) and see that the service is up.
"""
from __future__ import annotations

import html
from typing import Optional

from .config import Settings
from .seam import Identity


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def render_dev_login() -> str:
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>llmao · sign in (dev)</title>
{_STYLE}
</head><body>
<main class="shell">
  <div class="console">
    <div class="eyebrow">llmao · dev auth</div>
    <h1>Stand in as an ASF identity</h1>
    <p class="lede">No external calls in dev mode. Enter a uid and the projects it belongs to;
    PMC memberships grant admin on those projects.</p>
    <form method="post" action="/auth/dev/login" class="stack">
      <label>uid
        <input name="uid" placeholder="jdoe" autocomplete="off" required>
      </label>
      <label>committer projects <span class="hint">comma-separated</span>
        <input name="projects" placeholder="airflow, lineage" autocomplete="off">
      </label>
      <label>PMC memberships <span class="hint">comma-separated · grants admin</span>
        <input name="committees" placeholder="airflow" autocomplete="off">
      </label>
      <button type="submit">Sign in</button>
    </form>
  </div>
</main>
</body></html>"""


def render_index(settings: Settings, ident: Optional[Identity]) -> str:
    if ident is None:
        signin = (
            '<a class="btn" href="/auth/dev/login">Sign in (dev)</a>'
            if settings.is_dev_auth
            else '<a class="btn" href="/auth?login=/">Sign in with ASF</a>'
        )
        who = ""
    else:
        signin = '<a class="btn subtle" href="/auth/logout">Sign out</a>'
        projects = list(dict.fromkeys([*ident.committees, *ident.projects]))
        who = (
            f'<p class="meta"><span class="mono">{_esc(ident.uid)}</span> · '
            f'projects: {_esc(", ".join(projects) or "—")}</p>'
        )

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>llmao · control plane</title>
{_STYLE}
</head><body>
<main class="shell">
  <div class="console">
    <div class="eyebrow">llm.apache.org</div>
    <h1>llmao</h1>
    <p class="lede">Control plane for ASF LiteLLM access: project teams, budgets, and
    (soon) virtual keys. This service does <strong>not</strong> host a chat client —
    point your tools at the LiteLLM proxy with a project key.</p>
    {who}
    {signin}
    <div class="modeline">auth: {_esc(settings.auth_mode)} · backend: {_esc(settings.litellm_mode)}</div>
  </div>
</main>
</body></html>"""


_STYLE = """
<style>
  :root {
    --bg: #f4f5f2;
    --ink: #1a1c19;
    --muted: #5c635a;
    --line: #c8cdc4;
    --accent: #2f6f4e;
    --card: #fbfcf9;
    --mono: "IBM Plex Mono", ui-monospace, monospace;
    --sans: "IBM Plex Sans", system-ui, sans-serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh;
    font-family: var(--sans); color: var(--ink);
    background: var(--bg);
  }
  .shell {
    min-height: 100vh; display: grid; place-items: center;
    padding: 2rem 1rem;
  }
  .console {
    width: min(36rem, 100%);
    background: var(--card);
    border: 1px solid var(--line);
    box-shadow: 0 0 0 1px rgba(47, 111, 78, 0.06);
    padding: 1.75rem 1.75rem 1.4rem;
  }
  .eyebrow {
    font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--accent); margin-bottom: 0.6rem;
  }
  h1 { font-size: 1.65rem; font-weight: 600; margin: 0 0 0.6rem; letter-spacing: -0.02em; }
  .lede { color: var(--muted); line-height: 1.5; margin: 0 0 1.25rem; }
  .meta { margin: 0 0 1rem; color: var(--ink); font-size: 0.95rem; }
  .mono { font-family: var(--mono); font-size: 0.9em; }
  .modeline {
    margin-top: 1.25rem; padding-top: 0.85rem; border-top: 1px solid var(--line);
    font-family: var(--mono); font-size: 0.72rem; color: var(--muted);
  }
  .stack { display: grid; gap: 0.85rem; }
  label { display: grid; gap: 0.3rem; font-size: 0.85rem; font-weight: 500; }
  .hint { font-weight: 400; color: var(--muted); font-size: 0.8em; }
  input {
    font: inherit; padding: 0.55rem 0.65rem;
    border: 1px solid var(--line); background: #fff; color: var(--ink);
  }
  input:focus { outline: 2px solid color-mix(in srgb, var(--accent) 40%, transparent); border-color: var(--accent); }
  button, .btn {
    display: inline-block; font: inherit; font-weight: 500;
    padding: 0.55rem 0.95rem; border: 1px solid var(--accent);
    background: var(--accent); color: #fff; text-decoration: none; cursor: pointer;
  }
  .btn.subtle { background: transparent; color: var(--accent); }
  button:hover, .btn:hover { filter: brightness(0.95); }
</style>
"""

# Authoring Help pages

`docs/help/*.md` is rendered on each request by `pages.py`. `index.md` is `/help`. Any other file is `/help/<slug>`. Raw HTML in those files is left intact, because the files are committed here. Images belong under `static/` and are referenced as `/static/...`.

User-facing pages name `https://llm.apache.org` and the OpenAI API. They do not name LiteLLM.

## Mermaid, when we want it

This is not implemented. A fenced block

    ```mermaid
    flowchart LR
      A --> B
    ```

is emitted as `<pre><code class="language-mermaid">`. The help template would then load `/static/js/mermaid.min.js` and call `mermaid.run()` on those blocks. The script file is added with the first diagram.

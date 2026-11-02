# Authoring Help pages

`docs/help/*.md` is rendered on each request by `pages.py`. `index.md` is `/help`. Any other file is `/help/<slug>`. Raw HTML in those files is left intact, because the files are committed here. Images belong under `static/` and are referenced as `/static/...`.

A file that contains `[if-any`, `[repo]` or `[commit]` is run through EZT before Markdown. The data is NOT the page's `basic_info`; an article gets three fields only: `uid` (set when someone is signed in, None otherwise), `repo` (the GitHub URL) and `commit` (the running commit, which is how `terms.md` names the version being served). Write a literal `[` in that file as `[[]]`, which is how a Markdown link survives: `[[]My Keys](/keys)`.

`faq.md` and `terms.md` are not guides: `HELP_UNLISTED` in `pages.py` keeps them out of the Help nav. The FAQ is linked from the nav directly, the terms from the footer.

User-facing pages name `https://llm.apache.org` and the OpenAI API. They do not name LiteLLM.

## Mermaid, when we want it

This is not implemented. A fenced block

    ```mermaid
    flowchart LR
      A --> B
    ```

is emitted as `<pre><code class="language-mermaid">`. The help template would then load `/static/js/mermaid.min.js` and call `mermaid.run()` on those blocks. The script file is added with the first diagram.

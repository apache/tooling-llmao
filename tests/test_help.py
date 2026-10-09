"""Help markdown renders inside the page frame."""

import io
import pathlib

import asfquart
import ezt
from easydict import EasyDict as edict  # noqa: N813

THIS_DIR = pathlib.Path(__file__).resolve().parent.parent


def _pages():
    if asfquart.APP is None:
        asfquart.construct(
            "llmao", app_dir=str(THIS_DIR), cfg_file="config.yaml.example", oauth=False, force_login=False
        )
    import pages

    return pages


def test_article_renders_markdown_and_raw_html(tmp_path):
    (tmp_path / "index.md").write_text("# Overview\n\nHello <em>there</em>\n", encoding="utf-8")
    (tmp_path / "faq.md").write_text("# FAQ\n\nQuestions\n", encoding="utf-8")
    doc = _pages().help_article("index", tmp_path)
    assert doc.title == "Overview"
    assert "<em>there</em>" in doc.html
    assert "<h1>" in doc.html
    titles = [item.title for item in _pages().help_articles(tmp_path)]
    assert titles == ["FAQ"]


def test_unknown_slug_is_missing(tmp_path):
    assert _pages().help_article("../secret", tmp_path) is None
    assert _pages().help_article("missing", tmp_path) is None


def test_help_template_uses_the_page_frame(tmp_path):
    (tmp_path / "index.md").write_text("# Overview\n\nBody text\n", encoding="utf-8")
    doc = _pages().help_article("index", tmp_path)
    data = edict(
        title=doc.title,
        body=doc.html,
        guides=[edict(slug="pi", title="Pi")],
        flashes=[],
        uid=None,
        name=None,
        is_site_admin=ezt.boolean(False),
        restarted_at="",
        repo="https://example.test/llmao",
        commit="abc",
        can_create_automation=ezt.boolean(False),
    )
    template = ezt.Template(str(THIS_DIR / "templates" / "help.ezt"), base_format=ezt.FORMAT_HTML)
    buf = io.StringIO()
    template.generate(buf, data)
    out = buf.getvalue()
    assert "<h1>Overview</h1>" in out
    assert "&lt;h1&gt;" not in out
    assert "Body text" in out
    assert 'href="/help"' in out
    assert "Apache LLMAO" in out
    assert 'href="/help/faq"' in out


def test_index_links_each_tool_page():
    help_dir = THIS_DIR / "docs" / "help"
    index = (help_dir / "index.md").read_text(encoding="utf-8")
    for slug in ("claude-code", "codex", "grok-build", "pi"):
        assert f"/help/{slug}" in index
        assert (help_dir / f"{slug}.md").is_file()
    faq = (help_dir / "faq.md").read_text(encoding="utf-8")
    assert "/help/claude-code" in faq
    prose = "\n".join(path.read_text(encoding="utf-8") for path in help_dir.glob("*.md"))
    assert "LiteLLM" not in prose
    assert "Anthropic" not in prose


def test_home_page_does_not_list_api_paths():
    text = (THIS_DIR / "templates" / "home.ezt").read_text(encoding="utf-8")
    assert "/v1/projects" not in text
    assert "/healthz" not in text
    assert "LiteLLM" not in text
    assert "llm.apache.org" in text

"""Tests for Telegram Markdown → HTML conversion."""

from pocket_agent.channels.telegram import (
    md_to_telegram_html,
    prepare_telegram_message,
    sanitize_telegram_html,
)


def test_angle_brackets_escaped():
    out = prepare_telegram_message("Use x < y and z > 0")
    assert "&lt;" in out
    assert "<y" not in out


def test_code_block_preserves_content():
    out = prepare_telegram_message("```\nif a < b:\n    pass\n```")
    assert "<pre>" in out
    assert "if a &lt; b:" in out


def test_markdown_link():
    out = prepare_telegram_message("See [docs](https://example.com/a?b=1)")
    assert '<a href="https://example.com/a?b=1">' in out
    assert "docs</a>" in out


def test_bold_and_bullet():
    out = prepare_telegram_message("- **Status:** all good")
    assert "<b>Status:</b>" in out
    assert "▸" in out


def test_unclosed_bold_repaired():
    out = sanitize_telegram_html("<b>oops")
    assert out.endswith("</b>")


def test_strip_unknown_tags():
    out = sanitize_telegram_html("<div>hi</div><b>ok</b>")
    assert "<div>" not in out
    assert "<b>ok</b>" in out


def test_split_safe_across_chunks():
    from pocket_agent.channels.telegram import _split_message

    long = "word " * 900 + "**tail**"
    formatted = prepare_telegram_message(long)
    chunks = _split_message(formatted)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("<b>") == chunk.count("</b>")


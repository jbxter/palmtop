"""Tests for blog inline rendering escaping — issue #38."""

from __future__ import annotations

from palmtop.web.blog import _inline


def test_inline_escapes_raw_html():
    out = _inline("hello <script>alert(1)</script> world")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_inline_drops_javascript_link():
    out = _inline("[click](javascript:alert(1))")
    assert "javascript:" not in out
    assert "<a" not in out  # unsafe scheme → rendered as plain text
    assert "click" in out


def test_inline_drops_data_uri_link():
    out = _inline("[x](data:text/html,<script>alert(1)</script>)")
    assert "<a" not in out


def test_inline_allows_safe_link():
    assert '<a href="https://example.com">site</a>' in _inline("[site](https://example.com)")


def test_inline_still_formats_markdown():
    assert "<strong>x</strong>" in _inline("**x**")
    assert "<em>y</em>" in _inline("*y*")
    assert "<code>z</code>" in _inline("`z`")

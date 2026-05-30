"""Tests for the 12WY HTTP gateway base-URL pinning — issue #37."""

from __future__ import annotations

import pytest

from palmtop.mcp import twelvewy_http


def test_base_url_rejects_non_https():
    # The API key is sent as a bearer token, so a non-https base URL must be refused.
    twelvewy_http.configure("http://insecure.example.com", "twy_key")
    with pytest.raises(ValueError, match="https"):
        twelvewy_http._base_url()


def test_base_url_accepts_https_and_strips_slash():
    twelvewy_http.configure("https://app.up.railway.app/", "twy_key")
    assert twelvewy_http._base_url() == "https://app.up.railway.app"

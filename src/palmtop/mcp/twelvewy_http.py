"""HTTP client for 12WY remote mode (palmtop → Railway API)."""
from __future__ import annotations

import json
import os
from typing import Any

import httpx

_CLIENT = httpx.Client(timeout=60.0)
_override_base: str | None = None
_override_key: str | None = None


def configure(base_url: str, api_key: str) -> None:
    """Set API credentials (also reads TWELVEWY_* env vars when unset)."""
    global _override_base, _override_key
    _override_base = base_url.strip().rstrip("/")
    _override_key = api_key.strip()


def _base_url() -> str:
    url = _override_base or (os.environ.get("TWELVEWY_API_BASE_URL") or "").strip().rstrip("/")
    if not url:
        raise ValueError("TWELVEWY_API_BASE_URL is not set")
    return url


def _api_key() -> str:
    key = (
        _override_key
        or os.environ.get("TWELVEWY_API_KEY")
        or os.environ.get("AGENT_API_KEY")
        or ""
    ).strip()
    if not key:
        raise ValueError("Set TWELVEWY_API_KEY (or AGENT_API_KEY)")
    return key


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_api_key()}",
        "Accept": "application/json",
    }


def _request(method: str, path: str, body: dict | None = None) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    resp = _CLIENT.request(method, url, headers=_headers(), json=body)
    try:
        data = resp.json()
    except json.JSONDecodeError:
        data = {"message": resp.text}
    if resp.is_error:
        msg = data.get("message", data) if isinstance(data, dict) else data
        raise ValueError(f"API error {resp.status_code}: {msg}")
    return data


def get(path: str) -> dict[str, Any]:
    return _request("GET", path)


def post(path: str, body: dict | None = None) -> dict[str, Any]:
    return _request("POST", path, body)


def put(path: str, body: dict | None = None) -> dict[str, Any]:
    return _request("PUT", path, body)

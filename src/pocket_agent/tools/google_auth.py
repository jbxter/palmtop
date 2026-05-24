"""One-time OAuth2 helper for Google Calendar API.

Usage:
    python -m pocket_agent.tools.google_auth

Prerequisites:
    1. Go to https://console.cloud.google.com
    2. Create a project (or use an existing one)
    3. Enable the Google Calendar API
    4. Go to Credentials → Create Credentials → OAuth 2.0 Client ID
    5. Choose "Desktop app" as the application type
    6. Download the JSON and save as data/google_credentials.json

This script will:
    - Read your client credentials
    - Print a URL to authorize in your browser
    - Ask you to paste the authorization code
    - Exchange it for tokens and save to data/google_tokens.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlencode

import httpx

SCOPES = "https://www.googleapis.com/auth/calendar"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def run_auth(data_dir: Path) -> None:
    creds_path = data_dir / "google_credentials.json"
    tokens_path = data_dir / "google_tokens.json"

    if not creds_path.exists():
        print(f"Missing {creds_path}")
        print("Download your OAuth credentials from Google Cloud Console")
        print("and save them there. See this script's docstring for steps.")
        sys.exit(1)

    with open(creds_path) as f:
        creds = json.load(f)

    # Handle both "installed" and "web" credential types
    client_info = creds.get("installed") or creds.get("web")
    if not client_info:
        print("Invalid credentials file — expected 'installed' or 'web' key")
        sys.exit(1)

    client_id = client_info["client_id"]
    client_secret = client_info["client_secret"]
    redirect_uri = "urn:ietf:wg:oauth:2.0:oob"

    params = urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    })

    print(f"\nOpen this URL in your browser:\n\n{AUTH_URL}?{params}\n")
    code = input("Paste the authorization code here: ").strip()

    resp = httpx.post(TOKEN_URL, data={
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })

    if resp.status_code != 200:
        print(f"Token exchange failed: {resp.text}")
        sys.exit(1)

    tokens = resp.json()
    tokens_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tokens_path, "w") as f:
        json.dump(tokens, f, indent=2)

    print(f"\nTokens saved to {tokens_path}")
    print("the agent now has access to your Google Calendar.")


if __name__ == "__main__":
    data_dir = Path("data")
    if len(sys.argv) > 1:
        data_dir = Path(sys.argv[1])
    run_auth(data_dir)

"""Gemini Code Assist OAuth authentication.

Uses Google's OAuth client ID (same as gemini-cli) to authenticate
with the Code Assist API at cloudcode-pa.googleapis.com.

Token data stored in ~/.ragnarbot/oauth/gemini.json.
A marker is set in credentials.json (oauth_key) for quick auth checks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from ragnarbot.auth.oauth_flow import (
    refresh_token_request,
    run_oauth_flow,
)

CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = (
    "openid "
    "https://www.googleapis.com/auth/cloud-platform "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)
CODE_ASSIST_BASE = "https://cloudcode-pa.googleapis.com"
REDIRECT_PORT = 8585
CALLBACK_PATH = "/callback"

TOKEN_FILE = Path.home() / ".ragnarbot" / "oauth" / "gemini.json"


def authenticate(console) -> bool:
    """Run the full Gemini OAuth flow. Returns True on success."""
    console.print("\n  [bold]Gemini OAuth — Sign in with Google[/bold]\n")
    console.print("  Opening browser for Google sign-in...")

    tokens = run_oauth_flow(
        auth_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
        redirect_port=REDIRECT_PORT,
        callback_path=CALLBACK_PATH,
        extra_auth_params={"access_type": "offline", "prompt": "consent"},
    )

    if not tokens or "access_token" not in tokens:
        console.print("  [red]Authentication failed — no tokens received.[/red]")
        return False

    # Discover project ID via Code Assist API
    project_id = discover_project(tokens["access_token"])
    if not project_id:
        console.print("  [red]Failed to discover Code Assist project.[/red]")
        return False

    # Save token data to file
    expires_in = tokens.get("expires_in", 3600)
    _save_tokens({
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expiry": time.time() + expires_in,
        "project_id": project_id,
    })

    # Mark OAuth as configured in credentials
    _set_credentials_marker()

    console.print(f"  [green]Authenticated! Project: {project_id}[/green]")
    return True


def load_tokens() -> dict | None:
    """Load stored tokens from disk, or None if not found."""
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def refresh_if_needed() -> None:
    """Refresh the access token if expired."""
    tokens = load_tokens()
    if not tokens or not tokens.get("refresh_token"):
        return

    if time.time() < tokens.get("expiry", 0) - 60:
        return

    result = refresh_token_request(
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        refresh_token=tokens["refresh_token"],
        client_secret=CLIENT_SECRET,
    )

    if result and "access_token" in result:
        tokens["access_token"] = result["access_token"]
        tokens["expiry"] = time.time() + result.get("expires_in", 3600)
        if result.get("refresh_token"):
            tokens["refresh_token"] = result["refresh_token"]
        _save_tokens(tokens)


def get_access_token() -> str | None:
    """Load, refresh if needed, and return the access token."""
    refresh_if_needed()
    tokens = load_tokens()
    return tokens.get("access_token") if tokens else None


def get_project_id() -> str | None:
    """Return the stored project ID."""
    tokens = load_tokens()
    return tokens.get("project_id") if tokens else None


def discover_project(access_token: str) -> str | None:
    """Call the Code Assist loadCodeAssist endpoint to get the project ID."""
    url = f"{CODE_ASSIST_BASE}/v1internal:loadCodeAssist"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        resp = httpx.post(url, headers=headers, json={}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("cloudaicompanionProject")
            or data.get("billingProject")
            or data.get("project")
        )
    except httpx.HTTPError:
        return None


def is_authenticated() -> bool:
    """Check if Gemini OAuth tokens exist and have a refresh token."""
    tokens = load_tokens()
    return tokens is not None and bool(tokens.get("refresh_token"))


def _save_tokens(data: dict) -> None:
    """Persist token data to disk."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


def _set_credentials_marker() -> None:
    """Set oauth_key marker in credentials so auth checks pass."""
    from ragnarbot.auth.credentials import load_credentials, save_credentials
    creds = load_credentials()
    creds.providers.gemini.oauth_key = "configured"
    save_credentials(creds)

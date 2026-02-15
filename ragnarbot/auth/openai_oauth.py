"""OpenAI OAuth authentication via ChatGPT backend.

Uses the Codex CLI OAuth flow (PKCE via auth.openai.com) to obtain
an access_token used directly as Bearer with the ChatGPT backend API.

Token data stored in ~/.ragnarbot/oauth/openai.json.
A marker is set in credentials.json (oauth_key) for quick auth checks.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from ragnarbot.auth.oauth_flow import (
    refresh_token_request,
    run_oauth_flow,
)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
SCOPES = "openid profile email offline_access"
REDIRECT_PORT = 1455
CALLBACK_PATH = "/auth/callback"

TOKEN_FILE = Path.home() / ".ragnarbot" / "oauth" / "openai.json"


def authenticate(console) -> bool:
    """Run the full OpenAI OAuth flow. Returns True on success."""
    console.print("\n  [bold]OpenAI OAuth — Sign in with OpenAI[/bold]\n")
    console.print("  Opening browser for OpenAI sign-in...")

    tokens = run_oauth_flow(
        auth_url=AUTH_URL,
        token_url=TOKEN_URL,
        client_id=CLIENT_ID,
        scopes=SCOPES,
        redirect_port=REDIRECT_PORT,
        callback_path=CALLBACK_PATH,
        extra_auth_params={
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": "ragnarbot",
        },
    )

    if not tokens or "access_token" not in tokens:
        console.print("  [red]Authentication failed — no tokens received.[/red]")
        return False

    access_token = tokens["access_token"]

    # Extract account_id from JWT claims
    account_id = _extract_account_id(access_token)
    if not account_id:
        console.print("  [red]Failed to extract account ID from token.[/red]")
        return False

    # Save token data to file
    expires_in = tokens.get("expires_in", 3600)
    _save_tokens({
        "access_token": access_token,
        "refresh_token": tokens.get("refresh_token", ""),
        "expiry": time.time() + expires_in,
        "account_id": account_id,
    })

    # Mark OAuth as configured in credentials
    _set_credentials_marker()

    console.print("  [green]Authenticated with OpenAI![/green]")
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
    )

    if result and "access_token" in result:
        tokens["access_token"] = result["access_token"]
        tokens["expiry"] = time.time() + result.get("expires_in", 3600)
        if result.get("refresh_token"):
            tokens["refresh_token"] = result["refresh_token"]
        # Re-extract account_id from new token
        new_account_id = _extract_account_id(result["access_token"])
        if new_account_id:
            tokens["account_id"] = new_account_id
        _save_tokens(tokens)


def get_access_token() -> str | None:
    """Load, refresh if needed, and return the access token."""
    refresh_if_needed()
    tokens = load_tokens()
    return tokens.get("access_token") if tokens else None


def get_account_id() -> str | None:
    """Return the stored account ID."""
    tokens = load_tokens()
    return tokens.get("account_id") if tokens else None


def is_authenticated() -> bool:
    """Check if OpenAI OAuth tokens exist and have an access token."""
    tokens = load_tokens()
    return tokens is not None and bool(tokens.get("access_token"))


def _extract_account_id(token: str) -> str | None:
    """Extract chatgpt_account_id from a JWT access_token."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        auth_claim = claims.get("https://api.openai.com/auth", {})
        return auth_claim.get("chatgpt_account_id")
    except Exception:
        return None


def _save_tokens(data: dict) -> None:
    """Persist token data to disk."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(data, indent=2))
    TOKEN_FILE.chmod(0o600)


def _set_credentials_marker() -> None:
    """Set oauth_key marker in credentials so auth checks pass."""
    from ragnarbot.auth.credentials import load_credentials, save_credentials
    creds = load_credentials()
    creds.providers.openai.oauth_key = "configured"
    save_credentials(creds)

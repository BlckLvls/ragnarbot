"""Anthropic OAuth token refresh (no browser flow yet)."""

import time

import httpx

TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


async def refresh_access_token(refresh_token: str) -> dict:
    """Exchange refresh token for a new access token.

    Returns dict with keys: access_token, refresh_token, expires_in.
    Raises httpx.HTTPStatusError on failure.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        return resp.json()


async def ensure_valid_token(creds: "Credentials") -> str | None:  # noqa: F821
    """Check token expiry, refresh if needed, save updated credentials.

    Returns a valid access_token or None if no OAuth is configured.
    """
    from ragnarbot.auth.credentials import save_credentials

    provider = creds.providers.anthropic
    if not provider.oauth.access_token:
        return None

    # If no expiry set or not expired yet, return current token
    now = int(time.time())
    if provider.oauth.expires_at == 0 or now < provider.oauth.expires_at - 60:
        return provider.oauth.access_token

    # Need refresh
    if not provider.oauth.refresh_token:
        return provider.oauth.access_token  # Can't refresh, return stale token

    try:
        result = await refresh_access_token(provider.oauth.refresh_token)
        provider.oauth.access_token = result["access_token"]
        if "refresh_token" in result:
            provider.oauth.refresh_token = result["refresh_token"]
        if "expires_in" in result:
            provider.oauth.expires_at = now + result["expires_in"]
        save_credentials(creds)
        return provider.oauth.access_token
    except Exception:
        # Refresh failed, return existing token (may still work)
        return provider.oauth.access_token

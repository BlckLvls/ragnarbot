"""Shared OAuth PKCE utilities for browser-based OAuth flows."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

import httpx


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256).

    Returns (code_verifier, code_challenge).
    """
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def generate_state() -> str:
    """Generate a random state parameter for CSRF protection."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


class OAuthCallbackServer:
    """Lightweight HTTP server that captures the OAuth authorization code."""

    def __init__(self, port: int, callback_path: str, expected_state: str):
        self.port = port
        self.callback_path = callback_path
        self.expected_state = expected_state
        self.auth_code: str | None = None
        self.error: str | None = None
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path != outer.callback_path:
                    self.send_response(404)
                    self.end_headers()
                    return

                params = parse_qs(parsed.query)

                if params.get("error"):
                    outer.error = params["error"][0]
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h2>Authentication failed.</h2>"
                        b"<p>You can close this tab.</p></body></html>"
                    )
                    return

                state = params.get("state", [None])[0]
                if state != outer.expected_state:
                    outer.error = "State mismatch"
                    self.send_response(400)
                    self.end_headers()
                    return

                code = params.get("code", [None])[0]
                if not code:
                    outer.error = "No code received"
                    self.send_response(400)
                    self.end_headers()
                    return

                outer.auth_code = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authentication successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )

            def log_message(self, format, *args):
                pass  # Suppress HTTP server logs

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = Thread(target=self._server.handle_request, daemon=True)
        self._thread.start()

    def wait(self, timeout: float = 120) -> str | None:
        """Wait for the callback and return the auth code, or None on failure."""
        if self._thread:
            self._thread.join(timeout=timeout)
        if self._server:
            self._server.server_close()
        return self.auth_code

    def stop(self) -> None:
        if self._server:
            self._server.server_close()


def run_oauth_flow(
    *,
    auth_url: str,
    token_url: str,
    client_id: str,
    client_secret: str | None = None,
    scopes: str,
    redirect_port: int = 8585,
    callback_path: str = "/callback",
    extra_auth_params: dict | None = None,
) -> dict | None:
    """Run a complete OAuth authorization code + PKCE flow.

    Opens the browser, waits for callback, exchanges code for tokens.
    Returns the token response dict, or None on failure.
    """
    verifier, challenge = generate_pkce()
    state = generate_state()

    redirect_uri = f"http://localhost:{redirect_port}{callback_path}"

    # Build authorization URL
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if extra_auth_params:
        params.update(extra_auth_params)

    full_auth_url = f"{auth_url}?{urlencode(params)}"

    # Start callback server
    server = OAuthCallbackServer(redirect_port, callback_path, state)
    server.start()

    # Open browser
    webbrowser.open(full_auth_url)

    # Wait for callback
    code = server.wait(timeout=120)
    if not code:
        return None

    # Exchange code for tokens
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        token_data["client_secret"] = client_secret

    try:
        resp = httpx.post(token_url, data=token_data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None


def refresh_token_request(
    *,
    token_url: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
) -> dict | None:
    """Exchange a refresh_token for new tokens.

    Returns the token response dict, or None on failure.
    """
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret

    try:
        resp = httpx.post(token_url, data=data, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError:
        return None

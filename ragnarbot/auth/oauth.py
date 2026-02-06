"""Anthropic OAuth token helper.

Anthropic OAuth tokens (sk-ant-oat-*) are long-lived and do not require
refresh.  This module provides a single helper that returns a valid
access_token from credentials, or None when OAuth is not configured.
"""

from __future__ import annotations


def get_oauth_token(creds: "Credentials") -> str | None:  # noqa: F821
    """Return the OAuth access token for Anthropic, or None."""
    token = creds.providers.anthropic.oauth_key
    return token if token else None

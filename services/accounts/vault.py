"""Encrypts provider credentials before they are stored in the database.

The key is derived from APP_SECRET_KEY, which lives only in the server's .env — a
copy of the database alone doesn't reveal the API keys. Changing APP_SECRET_KEY makes
stored credentials unreadable; they then show as "needs re-linking" in the console and
the server falls back to the keys in .env (services/accounts/runtime.py).
"""
from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from config.settings import get_settings


@lru_cache(maxsize=4)
def _fernet_for(secret: str) -> Fernet:
    key = hashlib.pbkdf2_hmac("sha256", secret.encode(), b"voice-agent-provider-credentials", 200_000)
    return Fernet(base64.urlsafe_b64encode(key))


def _fernet() -> Fernet:
    return _fernet_for(get_settings().app_secret_key)


def seal(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def unseal(token: str) -> str | None:
    """The stored value, or None if it can't be decrypted (APP_SECRET_KEY changed)."""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def mask(value: str) -> str:
    """How a secret is shown back in the console: enough to recognise, never to use."""
    value = value.strip()
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}…{value[-4:]}"

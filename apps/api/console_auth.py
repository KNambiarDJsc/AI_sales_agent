"""Login for the client console (apps/api/routers/console.py).

One shared account (CONSOLE_USERNAME / CONSOLE_PASSWORD in .env) → a signed, expiring bearer token. Not
multi-user auth (apps/api/deps.py still flags that as future work) — but the console
can place phone calls at the account's expense, so it must never be reachable
without a secret. Tokens are HMAC-signed with a key derived from the password, so
changing the password logs everyone out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from config.settings import get_settings


def _key() -> bytes:
    password = get_settings().console_password
    return hashlib.sha256(f"voice-agent-console:{password}".encode()).digest()


def issue_token(now: float | None = None) -> str:
    expires = int((now or time.time()) + get_settings().console_token_hours * 3600)
    payload = str(expires).encode()
    sig = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + sig


def token_is_valid(token: str, now: float | None = None) -> bool:
    try:
        encoded, sig = token.split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        expires = int(payload)
    except (ValueError, TypeError):
        return False
    expected = hmac.new(_key(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected) and expires > (now or time.time())


def password_is_correct(password: str, username: str = "") -> bool:
    s = get_settings()
    if not s.console_password:
        return False
    user_ok = hmac.compare_digest(username.strip().lower().encode(), s.console_username.strip().lower().encode())
    return hmac.compare_digest(password.encode(), s.console_password.encode()) and user_ok


async def require_console(authorization: str | None = Header(default=None)) -> None:
    if not get_settings().console_password:
        raise HTTPException(status_code=503, detail="Console disabled: set CONSOLE_PASSWORD in .env")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not token or not token_is_valid(token):
        raise HTTPException(status_code=401, detail="Please log in")

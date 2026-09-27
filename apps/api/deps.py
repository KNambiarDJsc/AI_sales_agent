"""Shared FastAPI dependencies.

`get_current_tenant_id` is a PLACEHOLDER (Section 22 flags this explicitly): it trusts
an `X-Tenant-Id` header as-is. That is NOT authentication or authorization — it exists
only so every route already threads a tenant_id through the tenant-isolation-aware
repositories, instead of bolting tenant scoping on later. Replace this with real
auth (API keys / JWT / session cookies tied to the `user` table) before any real
lead/call data goes through this API.
"""
from __future__ import annotations

import uuid

from fastapi import Header, HTTPException


async def get_current_tenant_id(x_tenant_id: str | None = Header(default=None)) -> uuid.UUID:
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="Missing X-Tenant-Id header (auth not yet implemented — see apps/api/deps.py)")
    try:
        return uuid.UUID(x_tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="X-Tenant-Id must be a UUID") from None

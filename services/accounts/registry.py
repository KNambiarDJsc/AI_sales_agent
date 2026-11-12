"""The provider registry (config/providers.yaml) as typed objects."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from config.settings import REPO_ROOT

PROVIDERS_FILE = REPO_ROOT / "config" / "providers.yaml"


class Credential(BaseModel):
    field: str
    setting: str  # attribute of config.settings.Settings the adapters read
    label: str
    secret: bool = True


class Provider(BaseModel):
    id: str
    name: str
    role: str = ""
    does: str = ""
    required_for_calls: bool = False
    currency: Literal["INR", "USD"] = "INR"
    balance: Literal["api", "tracked"] = "tracked"
    recharge_url: str = ""
    dashboard_url: str = ""
    credentials: list[Credential] = Field(default_factory=list)
    help: str = ""


class Registry(BaseModel):
    usd_inr: float = 88.0
    providers: dict[str, Provider]
    pricing: dict


@lru_cache
def get_registry() -> Registry:
    with PROVIDERS_FILE.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    providers = {pid: Provider(id=pid, **cfg) for pid, cfg in (raw.get("providers") or {}).items()}
    return Registry(usd_inr=float(raw.get("usd_inr") or 88.0), providers=providers, pricing=raw.get("pricing") or {})

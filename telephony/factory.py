from __future__ import annotations

from functools import lru_cache

from config.settings import get_settings
from telephony.base import TelephonyProvider
from telephony.exotel import ExotelProvider
from telephony.freejun import FreejunProvider
from telephony.twilio import TwilioProvider

_PROVIDERS: dict[str, type[TelephonyProvider]] = {
    "twilio": TwilioProvider,
    "exotel": ExotelProvider,
    "freejun": FreejunProvider,
}


@lru_cache
def get_telephony_provider(name: str | None = None) -> TelephonyProvider:
    provider_name = name or get_settings().telephony_provider
    provider_cls = _PROVIDERS.get(provider_name)
    if provider_cls is None:
        raise ValueError(f"Unknown telephony provider: {provider_name!r}. Known: {list(_PROVIDERS)}")
    return provider_cls()

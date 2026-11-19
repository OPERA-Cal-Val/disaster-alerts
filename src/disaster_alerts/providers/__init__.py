from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List

from ..settings import Settings

# Import concrete providers (explicit is better than implicit).
# Keep these imports shallow; providers should handle their own deps.
from . import nws as _nws
from . import usgs as _usgs

log = logging.getLogger(__name__)

Event = Dict[str, Any]
ProviderFunc = Callable[[Settings], List[Event]]

# Canonical registry of supported providers, keyed to their module (not a
# direct function reference) so monkeypatching a module's fetch_events at
# test/runtime is honored via live attribute lookup in fetch_from_enabled.
REGISTRY: Dict[str, Any] = {
    "nws": _nws,
    "usgs": _usgs,
}

__all__ = ["Event", "ProviderFunc", "REGISTRY", "fetch_from_enabled"]


def _enabled_provider_keys(settings: Settings) -> List[str]:
    """Return the list of provider keys enabled in config (in a stable order)."""
    keys: List[str] = []
    try:
        if settings.app.providers.nws:
            keys.append("nws")
        if settings.app.providers.usgs:
            keys.append("usgs")
    except Exception:
        # If settings are malformed, fall back to all known providers.
        keys = list(REGISTRY.keys())
    return keys


def fetch_from_enabled(settings: Settings, lookback_days: float = 0.0) -> List[Event]:
    """
    Fetch events from all providers enabled in settings.app.providers.

    lookback_days > 0 asks providers for past events too (web map only).
    Returns a flat list of Event dicts. Failures in one provider do not prevent
    others from returning results; errors are logged and skipped.
    """
    kwargs = {"lookback_days": lookback_days} if lookback_days > 0 else {}
    results: List[Event] = []
    keys = _enabled_provider_keys(settings)
    if not keys:
        log.info("No providers enabled; returning empty event list")
        return results

    for key in keys:
        module = REGISTRY.get(key)
        fn = getattr(module, "fetch_events", None) if module is not None else None
        if fn is None:
            log.warning("Provider '%s' is not registered; skipping", key)
            continue
        try:
            evs = fn(settings, **kwargs)
        except Exception as e:
            log.error("Provider '%s' failed: %s", key, e, exc_info=False)
            continue
        if not isinstance(evs, list):
            log.warning("Provider '%s' returned non-list result; skipping", key)
            continue
        # Optionally tag provider key if a fetcher forgot (defensive)
        for e in evs:
            if isinstance(e, dict) and "provider" not in e:
                e["provider"] = key
        results.extend(evs)

    log.info("Fetched total %d event(s) from %d provider(s)", len(results), len(keys))
    return results

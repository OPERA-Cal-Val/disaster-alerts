from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ..settings import Settings
from .common import get_json

log = logging.getLogger(__name__)

Event = Dict[str, Any]
USGS_FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_from_ms(ts_ms: Optional[int]) -> Optional[str]:
    """Convert ms-since-epoch to ISO8601 'Z' string."""
    if ts_ms is None:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    except Exception:
        return None


def _severity_from_mag(mag: Optional[float]) -> Optional[str]:
    """USGS-ish buckets; keep strings capitalized to match NWS style."""
    if mag is None:
        return None
    if mag < 3.0:
        return "Minor"
    if mag < 4.0:
        return "Light"
    if mag < 5.0:
        return "Moderate"
    if mag < 6.0:
        return "Strong"
    if mag < 7.0:
        return "Major"
    return "Great"


def _float_or_none(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


DEFAULT_LOOKBACK_DAYS = 7.0


def fetch_events(
    settings: Settings, lookback_days: float = DEFAULT_LOOKBACK_DAYS
) -> List[Event]:
    """Fetch recent USGS earthquakes and normalize into Event dicts."""
    # min magnitude from thresholds (fallback 2.5)
    minmag = 2.5
    try:
        eq = settings.thresholds.earthquake
        if eq and eq.min_magnitude is not None:
            minmag = float(eq.min_magnitude)
    except Exception:
        pass

    end = _utc_now()
    start = end - timedelta(days=lookback_days or DEFAULT_LOOKBACK_DAYS)

    params = {
        "format": "geojson",
        "starttime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "endtime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": f"{minmag:.1f}",
        "limit": "2000",
        # You could add "orderby": "time" if needed; default is time desc.
    }

    data = get_json(USGS_FDSN_URL, params=params)
    feats = data.get("features") or []
    if not isinstance(feats, list):
        log.warning("USGS response missing 'features' list")
        return []

    out: List[Event] = []
    for f in feats:
        try:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("id") or "").strip()
            props = f.get("properties") or {}
            if not isinstance(props, dict):
                props = {}
            geom = f.get("geometry")

            mag = _float_or_none(props.get("mag"))
            updated = _iso_from_ms(props.get("updated")) or _iso_from_ms(
                props.get("time")
            )
            title = props.get("title") or (
                f"M {mag:.1f}" if mag is not None else "Earthquake"
            )
            link = props.get("url") if isinstance(props.get("url"), str) else None

            # depth_km from geometry.coordinates[2] (USGS depth is km in GeoJSON)
            if isinstance(geom, dict):
                coords = geom.get("coordinates")
                if isinstance(coords, list) and len(coords) >= 3:
                    depth_km = _float_or_none(coords[2])
                    if depth_km is not None:
                        props.setdefault("depth_km", depth_km)

            props.setdefault("event", "Earthquake")

            ev: Event = {
                "id": fid or title,  # fallback to title if id somehow absent
                "provider": "usgs",
                "updated": updated,
                "title": title,
                "severity": _severity_from_mag(mag),
                "link": link,
                "geometry": geom,
                "properties": props,
                "routing_key": "default",
            }
            out.append(ev)
        except Exception as e:
            log.debug("Skipping malformed USGS feature: %s", e, exc_info=False)
            continue

    log.info("USGS: normalized %d recent event(s) (minmag=%.1f)", len(out), minmag)
    return out

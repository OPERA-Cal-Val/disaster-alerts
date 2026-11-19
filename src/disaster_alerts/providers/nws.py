from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from ..settings import Settings
from .common import get_json

log = logging.getLogger(__name__)

Event = Dict[str, Any]
NWS_ACTIVE_URL = "https://api.weather.gov/alerts/active"
NWS_ALERTS_URL = "https://api.weather.gov/alerts"
# api.weather.gov only serves about a week of past alerts.
NWS_MAX_LOOKBACK_DAYS = 7.0
HISTORY_PAGE_LIMIT = 500
HISTORY_MAX_PAGES = 20


def _pick_str(d: Dict[str, Any], *keys: str) -> Optional[str]:
    """Return the first non-empty string value for any of keys in dict d."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _preferred_link(feature: Dict[str, Any], props: Dict[str, Any]) -> Optional[str]:
    """
    Choose the most useful link for an alert.

    Preference:
    1) feature["id"] (NWS Feature URL)
    2) props["id"] / props["@id"] (also often a URL)
    3) props["url"] (sometimes present)
    4) fall back to first "references" URL if present
    """
    # 1–3
    link = (
        _pick_str(feature, "id")
        or _pick_str(props, "id", "@id")
        or _pick_str(props, "url")
    )
    if link:
        return link

    # 4) references can be a list of dicts or strings
    refs = props.get("references")
    if isinstance(refs, list) and refs:
        first = refs[0]
        if isinstance(first, str):
            return first.strip() or None
        if isinstance(first, dict):
            # Some shapes: {"identifier": "...", "sender": "...", "url": "..."}
            return _pick_str(first, "url", "identifier")
    return None


def _severity(props: Dict[str, Any]) -> Optional[str]:
    s = props.get("severity")
    return s if isinstance(s, str) and s.strip() else None


def _updated(props: Dict[str, Any]) -> Optional[str]:
    # Prefer effective/onset/sent. 'updated' and 'ends' are last-resort
    return _pick_str(props, "effective", "onset", "sent", "updated", "ends")


def _parse_time(s: Any) -> Optional[datetime]:
    if not isinstance(s, str) or not s.strip():
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _reference_ids(props: Dict[str, Any]) -> List[str]:
    out = []
    for r in props.get("references") or []:
        if isinstance(r, dict):
            rid = _pick_str(r, "@id", "identifier")
            if rid:
                out.append(rid)
    return out


def _normalize(feats: Iterable[Any]) -> List[Event]:
    out: List[Event] = []
    for f in feats:
        try:
            if not isinstance(f, dict):
                continue
            props = f.get("properties") or {}
            if not isinstance(props, dict):
                props = {}

            fid = (
                _pick_str(f, "id")
                or _pick_str(props, "id", "@id")
                or _pick_str(props, "event", "headline")
                or "nws-unknown"
            )

            title = _pick_str(props, "headline", "event") or "(NWS Alert)"
            sev = _severity(props)
            link = _preferred_link(f, props)

            ev: Event = {
                "id": fid,
                "provider": "nws",
                "updated": _updated(props),
                "title": title,
                "severity": sev,
                "link": link,
                "geometry": f.get("geometry"),
                "properties": props,  # keep full properties for downstream formatting
                # Pipeline may override routing later; still set a sensible default.
                "routing_key": (
                    "severe"
                    if (sev or "").lower() in {"severe", "extreme"}
                    else "default"
                ),
            }
            out.append(ev)
        except Exception as e:
            # Keep this quiet (debug) so a single malformed feature doesn't spam logs.
            log.debug("Skipping malformed NWS feature: %s", e, exc_info=False)
            continue
    return out


def _fetch_history(settings: Settings, days: float) -> List[Event]:
    """Every message (Alert/Update/Cancel) sent in the last `days` days."""
    start = datetime.now(timezone.utc) - timedelta(days=days)
    params: Optional[Dict[str, Any]] = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "actual",
        "limit": HISTORY_PAGE_LIMIT,
    }
    weather = settings.thresholds.weather
    if weather and weather.include_events:
        params["event"] = ",".join(weather.include_events)
    else:
        log.warning("NWS history: no include_events configured; fetching every type")

    feats: List[Any] = []
    url = NWS_ALERTS_URL
    for _ in range(HISTORY_MAX_PAGES):
        data = get_json(url, params=params)
        page = data.get("features")
        if not isinstance(page, list):
            log.warning(
                "NWS history: page fetch failed; keeping %d message(s)", len(feats)
            )
            break
        feats.extend(page)
        nxt = (data.get("pagination") or {}).get("next")
        if not page or not isinstance(nxt, str) or not nxt.startswith(NWS_ALERTS_URL):
            break
        url, params = nxt, None
    else:
        log.warning("NWS history: stopped after %d pages", HISTORY_MAX_PAGES)
    return _normalize(feats)


def _threads(messages: List[Event]) -> List[List[Event]]:
    """Group messages that reference each other into one warning each."""
    parent: Dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for m in messages:
        root = find(m["id"])
        for ref in _reference_ids(m["properties"]):
            parent[find(ref)] = root

    groups: Dict[str, List[Event]] = {}
    for m in {m["id"]: m for m in messages}.values():
        groups.setdefault(find(m["id"]), []).append(m)
    return list(groups.values())


def _sent(m: Event) -> datetime:
    return _parse_time(m["properties"].get("sent")) or datetime.min.replace(
        tzinfo=timezone.utc
    )


def _ended_event(thread: List[Event], now: datetime) -> Event:
    """One event for a finished warning: latest content, status and end time."""
    thread = sorted(thread, key=_sent)
    cancels = [m for m in thread if m["properties"].get("messageType") == "Cancel"]
    content = [m for m in thread if m["properties"].get("messageType") != "Cancel"]
    rep = dict(content[-1] if content else thread[-1])
    props = rep["properties"]
    if cancels:
        status, ended = "canceled", _sent(cancels[-1])
    else:
        status = "expired"
        ended = (
            _parse_time(props.get("ends")) or _parse_time(props.get("expires")) or now
        )
    rep["id"] = thread[-1]["id"]
    rep["status"] = status
    rep["ended"] = _iso(min(ended, now))
    return rep


def fetch_events(settings: Settings, lookback_days: float = 0.0) -> List[Event]:
    """
    Fetch active NWS alerts and normalize them into Event dicts.

    With lookback_days > 0, also return warnings that ended within that window
    (one event per warning, the latest message's content), tagged with
    status "expired" or "canceled" and an "ended" time. Returns [] on failure.
    """
    data = get_json(NWS_ACTIVE_URL)
    feats = data.get("features") or []
    if not isinstance(feats, list):
        log.warning("NWS response missing 'features' list")
        return []

    active = _normalize(feats)
    for e in active:
        e["status"] = "active"
    log.info("NWS: normalized %d active alert(s)", len(active))
    if lookback_days <= 0 or not active:
        return active

    days = min(lookback_days, NWS_MAX_LOOKBACK_DAYS)
    if lookback_days > NWS_MAX_LOOKBACK_DAYS:
        log.warning(
            "NWS keeps only %g days of alerts; capping lookback %g -> %g",
            NWS_MAX_LOOKBACK_DAYS,
            lookback_days,
            days,
        )
    history = _fetch_history(settings, days)
    active_ids = {e["id"] for e in active}
    now = datetime.now(timezone.utc)
    ended = [
        _ended_event(t, now)
        for t in _threads(history + active)
        if not any(m["id"] in active_ids for m in t)
    ]
    log.info(
        "NWS: %d message(s) in the last %g day(s) -> %d ended warning(s)",
        len(history),
        days,
        len(ended),
    )
    return active + ended

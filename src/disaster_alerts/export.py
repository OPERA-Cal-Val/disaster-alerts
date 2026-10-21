"""Build a static snapshot of filtered NWS/USGS events for the public web map.

Read-only: never touches dedup state, never sends email, never writes to data/.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from . import providers, rules
from .plot_html_map import _color_from_event_type, _detect_family
from .providers import common
from .providers.usgs import _iso_from_ms
from .settings import Settings

log = logging.getLogger(__name__)
Event = Dict[str, Any]

ZONE_URL_RE = re.compile(r"^https://api\.weather\.gov/zones/([a-z]+)/([A-Z0-9]+)$")
SIMPLIFY_DEG = 0.005
COORD_DECIMALS = 4
MAX_TEXT_CHARS = 1500
ZONE_WORKERS = 6
USGS_LOOKBACK_DAYS = 7
USGS_DEFAULT_MIN_MAGNITUDE = 2.5


class SnapshotError(RuntimeError):
    pass


def _round_coords(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, COORD_DECIMALS)
    if isinstance(obj, (list, tuple)):
        return [_round_coords(v) for v in obj]
    return obj


def _polygonal(geom):
    """Keep only polygon parts; some NWS zones are GeometryCollections."""
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return geom
    if geom.geom_type == "GeometryCollection":
        parts = [_polygonal(g) for g in geom.geoms]
        parts = [p for p in parts if p is not None and not p.is_empty]
        return unary_union(parts) if parts else None
    return None


def _clean_geometry(geom) -> Optional[Dict[str, Any]]:
    geom = _polygonal(geom)
    if geom is None:
        return None
    geom = geom.simplify(SIMPLIFY_DEG, preserve_topology=True)
    if geom.is_empty:
        return None
    out = mapping(geom)
    return {"type": out["type"], "coordinates": _round_coords(out["coordinates"])}


def _zone_key(url: str) -> Optional[str]:
    m = ZONE_URL_RE.match(url.strip()) if isinstance(url, str) else None
    return f"{m.group(1)}_{m.group(2)}" if m else None


def _fetch_zone(url: str) -> Optional[Dict[str, Any]]:
    data = common.get_json(url)
    geom = data.get("geometry") if isinstance(data, dict) else None
    if not geom:
        return None
    try:
        return _clean_geometry(shape(geom))
    except Exception as exc:
        log.warning("Bad zone geometry from %s: %s", url, exc)
        return None


class ZoneCache:
    """NWS zone outlines, cached on disk because zones change very rarely."""

    def __init__(self, cache_dir: Optional[Path] = None):
        self.dir = cache_dir
        self.geoms: Dict[str, Dict[str, Any]] = {}
        self.hits = self.fetched = self.failed = 0

    def _read(self, key: str) -> Optional[Dict[str, Any]]:
        if self.dir is None:
            return None
        try:
            return json.loads((self.dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, key: str, geom: Dict[str, Any]) -> None:
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{key}.json").write_text(json.dumps(geom), encoding="utf-8")

    def load_many(self, urls: Iterable[str]) -> None:
        todo: Dict[str, str] = {}
        for url in urls:
            key = _zone_key(url)
            if key is None:
                log.warning("Ignoring unexpected zone URL %r", url)
                continue
            if key in self.geoms or key in todo:
                continue
            cached = self._read(key)
            if cached is not None:
                self.geoms[key] = cached
                self.hits += 1
            else:
                todo[key] = url
        if not todo:
            return
        with ThreadPoolExecutor(ZONE_WORKERS) as pool:
            for key, geom in zip(todo, pool.map(_fetch_zone, todo.values())):
                if geom is None:
                    self.failed += 1
                    continue
                self.geoms[key] = geom
                self.fetched += 1
                self._write(key, geom)

    def union(self, urls: Iterable[str]) -> Optional[Dict[str, Any]]:
        parts = [
            shape(self.geoms[k])
            for k in (_zone_key(u) for u in urls)
            if k in self.geoms
        ]
        return _clean_geometry(unary_union(parts)) if parts else None


def _affected_zones(e: Event) -> List[str]:
    zones = (e.get("properties") or {}).get("affectedZones") or []
    return [z for z in zones if isinstance(z, str)]


def _event_geometry(
    e: Event, zones: ZoneCache
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    geom = e.get("geometry")
    if isinstance(geom, dict) and geom.get("type"):
        if geom["type"] == "Point":
            lon, lat = geom["coordinates"][:2]
            point = {"type": "Point", "coordinates": _round_coords([lon, lat])}
            return point, "point"
        return _clean_geometry(shape(geom)), "alert"
    if e.get("provider") == "nws":
        merged = zones.union(_affected_zones(e))
        if merged:
            return merged, "zones"
    return None, None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _truncate(text: Any) -> Optional[str]:
    if not isinstance(text, str) or not text.strip():
        return None
    text = text.strip()
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…"


def event_type(e: Event) -> str:
    props = e.get("properties") or {}
    return str(props.get("event") or e.get("title") or "").strip() or "Unknown"


def _feature(
    e: Event, geometry: Optional[Dict[str, Any]], source: Optional[str]
) -> Dict[str, Any]:
    props = e.get("properties") or {}
    provider = str(e.get("provider") or "").lower()
    etype = event_type(e)
    out: Dict[str, Any] = {
        "id": e.get("id"),
        "provider": provider,
        "event": etype,
        "family": _detect_family(etype),
        "color": _color_from_event_type(etype),
        "severity": e.get("severity"),
        "title": e.get("title"),
        "link": e.get("link"),
        "updated": e.get("updated"),
        "geometry_source": source,
    }
    if provider == "usgs":
        out.update(
            time=_iso_from_ms(props.get("time")),
            magnitude=_num(props.get("mag")),
            depth_km=_num(props.get("depth_km")),
            place=props.get("place"),
            tsunami=bool(props.get("tsunami")),
            pager_alert=props.get("alert"),
        )
    else:
        out.update(
            time=props.get("onset") or props.get("effective") or props.get("sent"),
            expires=props.get("ends") or props.get("expires"),
            area=props.get("areaDesc"),
            headline=props.get("headline"),
            sender=props.get("senderName"),
            urgency=props.get("urgency"),
            certainty=props.get("certainty"),
            description=_truncate(props.get("description")),
            instruction=_truncate(props.get("instruction")),
        )
    return {
        "type": "Feature",
        "id": e.get("id"),
        "geometry": geometry,
        "properties": {k: v for k, v in out.items() if v is not None and v != ""},
    }


def _usgs_query_min_magnitude(settings: Settings) -> float:
    eq = settings.thresholds.earthquake
    if eq and eq.min_magnitude is not None:
        return float(eq.min_magnitude)
    return USGS_DEFAULT_MIN_MAGNITUDE


def _filters(settings: Settings) -> Dict[str, Any]:
    """The rules the browser re-applies to live feeds, so both views agree."""
    thr = settings.thresholds
    return {
        "min_severity": thr.global_.min_severity,
        "earthquake": thr.earthquake.model_dump() if thr.earthquake else None,
        "weather": (
            {
                "include_events": thr.weather.include_events,
                "exclude_events": thr.weather.exclude_events,
            }
            if thr.weather
            else None
        ),
        "aoi": settings.app.aoi,
        "usgs_lookback_days": USGS_LOOKBACK_DAYS,
        "usgs_query_min_magnitude": _usgs_query_min_magnitude(settings),
    }


def _colors(settings: Settings, features: List[Dict[str, Any]]) -> Dict[str, str]:
    names = {"Earthquake"}
    if settings.thresholds.weather:
        names.update(settings.thresholds.weather.include_events)
    names.update(f["properties"]["event"] for f in features)
    return {n: _color_from_event_type(n) for n in sorted(names)}


def build_snapshot(
    settings: Settings, zones: Optional[ZoneCache] = None
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    zones = zones or ZoneCache()
    fetched = providers.fetch_from_enabled(settings)
    fetched = [e for e in fetched if isinstance(e, dict) and "id" in e]
    raw_counts = Counter(str(e.get("provider")) for e in fetched)
    if "nws" in settings.enabled_providers and not raw_counts.get("nws"):
        # NWS always has active alerts nationwide; zero means the fetch failed.
        raise SnapshotError("NWS returned no alerts; refusing to publish a snapshot")

    events = rules.filter_events(fetched, settings.thresholds, settings.app.aoi)
    zones.load_many(
        z for e in events if not e.get("geometry") for z in _affected_zones(e)
    )

    features = []
    for e in events:
        try:
            geometry, source = _event_geometry(e, zones)
        except Exception as exc:
            log.warning("Could not build geometry for %s: %s", e.get("id"), exc)
            geometry, source = None, None
        features.append(_feature(e, geometry, source))
    features.sort(key=lambda f: f["properties"].get("time") or "", reverse=True)

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_events": len(features),
        "n_without_geometry": sum(1 for f in features if f["geometry"] is None),
        "providers": settings.enabled_providers,
        "fetched": dict(raw_counts),
        "by_event": dict(Counter(f["properties"]["event"] for f in features)),
        "zones": {
            "cached": zones.hits,
            "fetched": zones.fetched,
            "failed": zones.failed,
        },
        "filters": _filters(settings),
        "colors": _colors(settings, features),
    }
    return {"type": "FeatureCollection", "features": features}, meta


def _write_json(path: Path, payload: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def write_snapshot(
    collection: Dict[str, Any], meta: Dict[str, Any], output_dir: Path
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    events_path = output_dir / "events.geojson"
    _write_json(events_path, collection)
    _write_json(output_dir / "events.meta.json", meta)
    return events_path


def _parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="disaster-alerts-export",
        description=(
            "Write a static GeoJSON snapshot of filtered events for the web map."
        ),
    )
    p.add_argument("-o", "--output", type=Path, default=Path("site/data"))
    p.add_argument(
        "--zone-cache",
        type=Path,
        default=Path(".cache/nws_zones"),
        help="Directory for cached NWS zone outlines.",
    )
    p.add_argument("--root", type=Path, help="Repo root holding config/.")
    p.add_argument(
        "-l",
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.load(root=args.root)
    zones = ZoneCache(args.zone_cache)
    try:
        collection, meta = build_snapshot(settings, zones)
    except SnapshotError as exc:
        log.error("%s", exc)
        return 1
    path = write_snapshot(collection, meta, args.output)
    log.info(
        "Wrote %d event(s) to %s (zones: %s)", meta["n_events"], path, meta["zones"]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

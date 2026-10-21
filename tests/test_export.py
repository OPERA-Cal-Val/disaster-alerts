import json
from pathlib import Path

import pytest

from disaster_alerts import export
from disaster_alerts.settings import (
    EarthquakeThresholds,
    Thresholds,
    WeatherThresholds,
)

ZONE_A = "https://api.weather.gov/zones/forecast/AKZ838"
ZONE_B = "https://api.weather.gov/zones/forecast/VAZ098"


def _square(x, y, d=1.0):
    return {
        "type": "Polygon",
        "coordinates": [[[x, y], [x + d, y], [x + d, y + d], [x, y + d], [x, y]]],
    }


def _quake(eid, mag, lon=-120.0, lat=35.0):
    return {
        "id": eid,
        "provider": "usgs",
        "updated": "2026-10-21T10:00:00Z",
        "title": f"M {mag} - Somewhere",
        "severity": "Strong",
        "link": f"https://earthquake.usgs.gov/earthquakes/eventpage/{eid}",
        "geometry": {"type": "Point", "coordinates": [lon, lat, 12.0]},
        "properties": {
            "mag": mag,
            "depth_km": 12.0,
            "place": "10 km N of Somewhere, CA",
            "time": 1792576800000,
            "event": "Earthquake",
        },
    }


def _alert(eid, event, geometry=None, zones=None):
    return {
        "id": eid,
        "provider": "nws",
        "updated": "2026-10-21T03:00:00-08:00",
        "title": f"{event} issued",
        "severity": "Severe",
        "link": f"https://api.weather.gov/alerts/{eid}",
        "geometry": geometry,
        "properties": {
            "event": event,
            "areaDesc": "Some County",
            "onset": "2026-10-21T04:00:00-08:00",
            "ends": "2026-10-22T04:00:00-08:00",
            "description": "x" * 5000,
            "affectedZones": zones or [],
        },
    }


@pytest.fixture
def settings(settings_factory):
    s = settings_factory()
    s.thresholds = Thresholds(
        earthquake=EarthquakeThresholds(min_magnitude=5.0),
        weather=WeatherThresholds(
            include_events=["Flood Warning", "Storm Warning"],
            exclude_events=["Special Weather Statement"],
        ),
    )
    return s


@pytest.fixture
def zone_calls(monkeypatch):
    calls = []
    shapes = {
        ZONE_A: _square(-170, 50),
        # Some coastal zones come back as a GeometryCollection.
        ZONE_B: {
            "type": "GeometryCollection",
            "geometries": [
                _square(-80, 37),
                {"type": "LineString", "coordinates": [[-80, 37], [-79, 38]]},
            ],
        },
    }

    def fake_get_json(url, *a, **kw):
        calls.append(url)
        return {"geometry": shapes[url]} if url in shapes else {}

    monkeypatch.setattr("disaster_alerts.providers.common.get_json", fake_get_json)
    return calls


def test_snapshot_applies_rules_and_resolves_geometry(
    settings, patch_nws, patch_usgs, zone_calls
):
    patch_usgs([_quake("big", 6.1), _quake("small", 4.0)])
    patch_nws(
        [
            _alert("flood", "Flood Warning", geometry=_square(-90, 30)),
            _alert("storm", "Storm Warning", zones=[ZONE_A, ZONE_B]),
            _alert("sws", "Special Weather Statement", geometry=_square(-90, 30)),
        ]
    )

    collection, meta = export.build_snapshot(settings)

    by_id = {f["id"]: f for f in collection["features"]}
    assert set(by_id) == {"big", "flood", "storm"}
    assert by_id["big"]["geometry"] == {"type": "Point", "coordinates": [-120.0, 35.0]}
    assert by_id["big"]["properties"]["magnitude"] == 6.1
    assert by_id["big"]["properties"]["family"] == "earthquake"
    assert by_id["flood"]["properties"]["geometry_source"] == "alert"
    storm = by_id["storm"]
    assert storm["properties"]["geometry_source"] == "zones"
    assert storm["geometry"]["type"] == "MultiPolygon"
    assert len(storm["properties"]["description"]) <= export.MAX_TEXT_CHARS + 1
    assert sorted(zone_calls) == [ZONE_A, ZONE_B]

    assert meta["n_events"] == 3
    assert meta["fetched"] == {"usgs": 2, "nws": 3}
    assert meta["by_event"] == {"Earthquake": 1, "Flood Warning": 1, "Storm Warning": 1}
    assert meta["filters"]["earthquake"]["min_magnitude"] == 5.0
    assert meta["filters"]["weather"]["include_events"] == [
        "Flood Warning",
        "Storm Warning",
    ]
    assert set(meta["colors"]) >= {"Earthquake", "Flood Warning", "Storm Warning"}


def test_zone_cache_avoids_refetching(settings, patch_nws, patch_usgs, zone_calls):
    patch_usgs([])
    patch_nws([_alert("storm", "Storm Warning", zones=[ZONE_A])])
    cache_dir = settings.paths.root / "zones"

    export.build_snapshot(settings, export.ZoneCache(cache_dir))
    _, meta = export.build_snapshot(settings, export.ZoneCache(cache_dir))

    assert zone_calls == [ZONE_A]
    assert meta["zones"] == {"cached": 1, "fetched": 0, "failed": 0}


def test_untrusted_zone_urls_are_not_fetched(
    settings, patch_nws, patch_usgs, zone_calls
):
    patch_usgs([])
    patch_nws(
        [
            _alert(
                "storm",
                "Storm Warning",
                zones=[
                    "https://evil.example/zones/forecast/X",
                    "http://169.254.169.254/",
                ],
            )
        ]
    )

    collection, meta = export.build_snapshot(settings)

    assert zone_calls == []
    assert collection["features"][0]["geometry"] is None
    assert meta["n_without_geometry"] == 1


def test_empty_nws_feed_refuses_to_publish(settings, patch_nws, patch_usgs):
    patch_usgs([_quake("big", 6.1)])
    patch_nws([])
    with pytest.raises(export.SnapshotError):
        export.build_snapshot(settings)


def test_no_matching_events_still_writes_files(
    settings, patch_nws, patch_usgs, tmp_path: Path
):
    patch_usgs([_quake("small", 3.0)])
    patch_nws([_alert("sws", "Special Weather Statement")])

    collection, meta = export.build_snapshot(settings)
    export.write_snapshot(collection, meta, tmp_path / "site" / "data")

    events = json.loads((tmp_path / "site/data/events.geojson").read_text())
    saved_meta = json.loads((tmp_path / "site/data/events.meta.json").read_text())
    assert events == {"type": "FeatureCollection", "features": []}
    assert saved_meta["n_events"] == 0
    assert saved_meta["generated_at"]


def test_export_never_touches_state_or_email(
    settings, patch_nws, patch_usgs, monkeypatch, tmp_path: Path
):
    def boom(*a, **kw):
        raise AssertionError("export must not send email")

    monkeypatch.setattr("disaster_alerts.email.send", boom)
    state_file = settings.paths.state_file
    before = state_file.read_text()
    patch_usgs([_quake("big", 6.1)])
    patch_nws([_alert("flood", "Flood Warning", geometry=_square(-90, 30))])

    collection, meta = export.build_snapshot(settings)
    export.write_snapshot(collection, meta, tmp_path / "out")

    assert state_file.read_text() == before
    assert sorted(p.name for p in settings.paths.data_dir.iterdir()) == ["state.json"]

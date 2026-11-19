from datetime import datetime, timedelta, timezone

import pytest

from disaster_alerts import providers
from disaster_alerts.providers import nws as nws_mod
from disaster_alerts.settings import Thresholds, WeatherThresholds

BASE = "https://api.weather.gov/alerts/urn:oid:"
NOW = datetime.now(timezone.utc)


def _ts(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _msg(mid, kind="Alert", refs=(), sent_h=10, ends_h=None, event="Flood Warning"):
    props = {
        "id": BASE + mid,
        "event": event,
        "headline": f"{event} {mid}",
        "severity": "Severe",
        "messageType": kind,
        "sent": _ts(sent_h),
        "references": [{"@id": BASE + r, "identifier": r} for r in refs],
    }
    if ends_h is not None:
        props["ends"] = _ts(ends_h)
    return {"id": BASE + mid, "geometry": None, "properties": props}


@pytest.fixture
def settings(settings_factory):
    s = settings_factory()
    s.thresholds = Thresholds(
        weather=WeatherThresholds(include_events=["Flood Warning", "Storm Warning"])
    )
    return s


@pytest.fixture
def feeds(monkeypatch):
    """Serve fake /alerts/active and paged /alerts responses; record calls."""
    state = {"active": [], "pages": [[]], "calls": []}

    def fake_get_json(url, params=None, **kw):
        state["calls"].append((url, params))
        if url == nws_mod.NWS_ACTIVE_URL:
            return {"features": state["active"]}
        idx = 0 if params else int(url.rsplit("page=", 1)[1])
        out = {"features": state["pages"][idx]}
        if idx + 1 < len(state["pages"]):
            out["pagination"] = {"next": f"{nws_mod.NWS_ALERTS_URL}?page={idx + 1}"}
        return out

    monkeypatch.setattr(nws_mod, "get_json", fake_get_json)
    return state


def test_no_lookback_fetches_active_only(settings, feeds):
    feeds["active"] = [_msg("a1")]

    events = nws_mod.fetch_events(settings)

    assert [e["status"] for e in events] == ["active"]
    assert [c[0] for c in feeds["calls"]] == [nws_mod.NWS_ACTIVE_URL]


def test_threads_merge_into_one_ended_warning(settings, feeds):
    feeds["active"] = [_msg("live")]
    feeds["pages"] = [
        [
            _msg("x1", sent_h=40, ends_h=20),
            _msg("x2", "Update", refs=["x1"], sent_h=30, ends_h=5),
            _msg("c1", sent_h=50),
            _msg("c2", "Cancel", refs=["c1"], sent_h=45),
        ]
    ]

    events = nws_mod.fetch_events(settings, lookback_days=3)

    by_id = {e["id"]: e for e in events}
    assert set(by_id) == {BASE + "live", BASE + "x2", BASE + "c2"}
    expired = by_id[BASE + "x2"]
    assert expired["status"] == "expired"
    assert expired["ended"] == _ts(5)
    assert expired["title"] == "Flood Warning x2"
    canceled = by_id[BASE + "c2"]
    assert canceled["status"] == "canceled"
    assert canceled["ended"] == _ts(45)
    # The content comes from the last non-cancel message, not the Cancel itself.
    assert canceled["title"] == "Flood Warning c1"

    url, params = feeds["calls"][1]
    assert url == nws_mod.NWS_ALERTS_URL
    assert params["event"] == "Flood Warning,Storm Warning"
    assert params["status"] == "actual"


def test_warning_still_active_is_not_duplicated(settings, feeds):
    feeds["active"] = [_msg("u2", "Update", refs=["u1"], sent_h=2)]
    feeds["pages"] = [
        [_msg("u1", sent_h=20), _msg("u2", "Update", refs=["u1"], sent_h=2)]
    ]

    events = nws_mod.fetch_events(settings, lookback_days=2)

    assert [(e["id"], e["status"]) for e in events] == [(BASE + "u2", "active")]


def test_lookback_is_capped_and_pages_are_followed(settings, feeds):
    feeds["active"] = [_msg("live")]
    feeds["pages"] = [[_msg("p1", ends_h=3)], [_msg("p2", ends_h=4)]]

    events = nws_mod.fetch_events(settings, lookback_days=30)

    assert {e["id"] for e in events if e["status"] == "expired"} == {
        BASE + "p1",
        BASE + "p2",
    }
    start = datetime.fromisoformat(feeds["calls"][1][1]["start"].replace("Z", "+00:00"))
    assert abs((NOW - start) - timedelta(days=nws_mod.NWS_MAX_LOOKBACK_DAYS)) < (
        timedelta(minutes=5)
    )


def test_history_failure_keeps_active_alerts(settings, monkeypatch):
    def fake_get_json(url, params=None, **kw):
        return {"features": [_msg("live")]} if url == nws_mod.NWS_ACTIVE_URL else {}

    monkeypatch.setattr(nws_mod, "get_json", fake_get_json)

    events = nws_mod.fetch_events(settings, lookback_days=7)

    assert [e["status"] for e in events] == ["active"]


def test_fetch_from_enabled_passes_lookback_only_when_set(settings, monkeypatch):
    seen = []
    monkeypatch.setattr(nws_mod, "fetch_events", lambda s, **kw: seen.append(kw) or [])
    monkeypatch.setattr(
        "disaster_alerts.providers.usgs.fetch_events", lambda s, **kw: []
    )

    providers.fetch_from_enabled(settings)
    providers.fetch_from_enabled(settings, 3)

    assert seen == [{}, {"lookback_days": 3}]

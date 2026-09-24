import importlib.util
import json
import threading
import time
from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("flask")
pytest.importorskip("shapely")

spec = importlib.util.spec_from_file_location(
    "test_web_app_module", Path(__file__).resolve().parents[1] / "web" / "app.py"
)
web_app = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(web_app)


@pytest.fixture(autouse=True)
def clear_processing_runs():
    with web_app.processing_runs_lock:
        web_app.processing_runs.clear()
    yield
    with web_app.processing_runs_lock:
        web_app.processing_runs.clear()


@pytest.fixture
def client():
    web_app.app.config["TESTING"] = True
    return web_app.app.test_client()


def test_process_bbox_returns_unique_run_ids_per_request(monkeypatch, client):
    started_threads = []

    class DummyThread:
        def __init__(self, target, args=(), daemon=None):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            started_threads.append((self.target, self.args, self.daemon))

    monkeypatch.setattr(web_app.threading, "Thread", DummyThread)

    payload = {
        "lat_min": 1,
        "lat_max": 2,
        "lon_min": 3,
        "lon_max": 4,
        "search_type": "overpasses",
    }

    response_a = client.post("/process_bbox", json=payload)
    response_b = client.post(
        "/process_bbox", json={**payload, "search_type": "opera_search"}
    )

    run_id_a = response_a.get_json()["run_id"]
    run_id_b = response_b.get_json()["run_id"]

    assert run_id_a != run_id_b
    assert started_threads[0][0] is web_app.run_overpasses_only
    assert started_threads[0][1] == (run_id_a, payload)
    assert started_threads[1][0] is web_app.run_opera_search
    assert started_threads[1][1] == (
        run_id_b,
        {
            **payload,
            "search_type": "opera_search",
            "functionality": "opera_search",
        },
    )
    assert web_app._get_run_state(run_id_a)["search_type"] == ["overpasses"]
    assert web_app._get_run_state(run_id_b)["search_type"] == ["opera_search"]


def test_processing_status_is_scoped_to_run_id(client):
    run_id_a = web_app._create_run("both")
    run_id_b = web_app._create_run("opera_search")

    web_app._update_run_state(run_id_a, running=True, error="run A failed")
    web_app._update_run_state(run_id_b, running=False, error=None)

    response_a = client.get("/processing_status", query_string={"run_id": run_id_a})
    response_b = client.get("/processing_status", query_string={"run_id": run_id_b})

    assert response_a.status_code == 200
    body_a = response_a.get_json()
    assert body_a["running"] is True
    assert body_a["error"] == "run A failed"
    # Progress fields are present but empty for a run that hasn't reported yet.
    assert body_a["stage"] is None
    assert body_a["progress"] is None
    assert isinstance(body_a["elapsed_seconds"], (int, float))

    assert response_b.status_code == 200
    body_b = response_b.get_json()
    assert body_b["running"] is False
    assert body_b["error"] is None


def test_show_maps_and_map_assets_use_run_specific_output_folder(client, tmp_path):
    run_id_a = web_app._create_run("overpasses")
    run_id_b = web_app._create_run("opera_search")
    folder_a = tmp_path / "nextpass_outputs_a"
    folder_b = tmp_path / "nextpass_outputs_b"
    folder_a.mkdir()
    folder_b.mkdir()

    (folder_a / "run_output.txt").write_text("A log", encoding="utf-8")
    (folder_b / "run_output.txt").write_text("B log", encoding="utf-8")
    (folder_a / "satellite_overpasses_map.html").write_text("sat-a", encoding="utf-8")
    (folder_b / "opera_products_map.html").write_text("opera-b", encoding="utf-8")

    web_app._update_run_state(run_id_a, running=False, latest_folder=str(folder_a))
    web_app._update_run_state(run_id_b, running=False, latest_folder=str(folder_b))

    show_a = client.get("/show_maps", query_string={"run_id": run_id_a})
    show_b = client.get("/show_maps", query_string={"run_id": run_id_b})
    map_a = client.get(f"/maps/{run_id_a}/satellite_overpasses_map.html")
    map_b = client.get(f"/maps/{run_id_b}/opera_products_map.html")

    body_a = show_a.get_data(as_text=True)
    body_b = show_b.get_data(as_text=True)

    assert show_a.status_code == 200
    assert f"/maps/{run_id_a}/satellite_overpasses_map.html" in body_a
    assert "/opera_products_map.html" not in body_a
    assert show_b.status_code == 200
    assert f"/maps/{run_id_b}/opera_products_map.html" in body_b
    assert "/satellite_overpasses_map.html" not in body_b
    assert map_a.get_data(as_text=True) == "sat-a"
    assert map_b.get_data(as_text=True) == "opera-b"


def test_processing_status_rejects_unknown_run_id(client):
    response = client.get("/processing_status", query_string={"run_id": "missing"})

    assert response.status_code == 404
    assert response.get_json() == {"error": "Unknown or missing run_id"}


# --- New AOI-input tests ----------------------------------------------------


class _CapturingThread:
    """Thread stub that records target/args and never runs the target."""

    started = []

    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args
        self.daemon = daemon

    def start(self):
        _CapturingThread.started.append((self.target, self.args, self.daemon))


@pytest.fixture
def capture_threads(monkeypatch):
    _CapturingThread.started = []
    monkeypatch.setattr(web_app.threading, "Thread", _CapturingThread)
    return _CapturingThread


def test_process_bbox_accepts_wkt_polygon(capture_threads, client):
    payload = {
        "aoi": {
            "kind": "wkt",
            "value": "POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))",
        },
        "search_type": "overpasses",
    }
    resp = client.post("/process_bbox", json=payload)
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]

    # Bounds derived on the server; passed to run_overpasses_only via run_state.
    state = web_app._get_run_state(run_id)
    assert state["np_bbox_arg"] == ["POLYGON((0 0, 1 0, 1 1, 0 1, 0 0))"]

    # Server populated bbox keys on the params dict for downstream consumers.
    params_passed = capture_threads.started[0][1][1]
    assert params_passed["lat_min"] == 0
    assert params_passed["lat_max"] == 1
    assert params_passed["lon_min"] == 0
    assert params_passed["lon_max"] == 1


def test_process_bbox_accepts_wkt_point_and_inflates(capture_threads, client):
    payload = {
        "aoi": {"kind": "wkt", "value": "POINT(10 20)"},
        "search_type": "overpasses",
    }
    resp = client.post("/process_bbox", json=payload)
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]

    # Rich form passed unchanged to next_pass.
    assert web_app._get_run_state(run_id)["np_bbox_arg"] == ["POINT(10 20)"]

    # Zero-area bbox from POINT gets inflated for the disasters consumer.
    params_passed = capture_threads.started[0][1][1]
    assert params_passed["lat_max"] > params_passed["lat_min"]
    assert params_passed["lon_max"] > params_passed["lon_min"]


def test_process_bbox_draw_point_inflates_despite_client_bbox(capture_threads, client):
    """The map's point tool mirrors its collapsed bbox into the top-level keys.

    Those must still be inflated: run_disasters turns them into the mosaic
    master grid, where zero extent produces a 1x1 pixel product.
    """
    lat, lon = 41.5086, -94.1748
    payload = {
        "aoi": {
            "kind": "draw",
            "value": {
                "lat_min": lat,
                "lat_max": lat,
                "lon_min": lon,
                "lon_max": lon,
            },
        },
        "lat_min": lat,
        "lat_max": lat,
        "lon_min": lon,
        "lon_max": lon,
        "search_type": "disasters",
    }
    resp = client.post("/process_bbox", json=payload)
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]

    params_passed = capture_threads.started[0][1][1]
    assert params_passed["lat_max"] > params_passed["lat_min"]
    assert params_passed["lon_max"] > params_passed["lon_min"]

    # next_pass takes the exact point instead: two tokens is its point form.
    assert web_app._get_run_state(run_id)["np_bbox_arg"] == [str(lat), str(lon)]


def test_process_bbox_draw_rectangle_keeps_client_bbox(capture_threads, client):
    """A non-degenerate drawn box stays byte-identical for existing callers."""
    payload = {
        "aoi": {
            "kind": "draw",
            "value": {
                "lat_min": 1,
                "lat_max": 2,
                "lon_min": 3,
                "lon_max": 4,
            },
        },
        "lat_min": 1,
        "lat_max": 2,
        "lon_min": 3,
        "lon_max": 4,
        "search_type": "overpasses",
    }
    resp = client.post("/process_bbox", json=payload)
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]

    params_passed = capture_threads.started[0][1][1]
    assert [
        params_passed["lat_min"],
        params_passed["lat_max"],
        params_passed["lon_min"],
        params_passed["lon_max"],
    ] == [1, 2, 3, 4]
    assert web_app._get_run_state(run_id)["np_bbox_arg"] == [
        "1.0",
        "2.0",
        "3.0",
        "4.0",
    ]


def test_process_bbox_accepts_geojson_file_upload(capture_threads, client, tmp_path):
    geojson_body = b'{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,3],[0,3],[0,0]]]}'
    payload = {"aoi": {"kind": "file", "value": None}, "search_type": "overpasses"}

    resp = client.post(
        "/process_bbox",
        data={
            "payload_json": json.dumps(payload),
            "aoi_file": (BytesIO(geojson_body), "aoi.geojson"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    run_id = resp.get_json()["run_id"]

    # Uploaded file lands in aoi_uploads_<run_id>/ and its absolute path is
    # what next_pass will receive after `-b`.
    np_arg = web_app._get_run_state(run_id)["np_bbox_arg"]
    assert len(np_arg) == 1
    saved_path = Path(np_arg[0])
    assert saved_path.exists()
    assert saved_path.name == "aoi.geojson"
    assert saved_path.parent.name == f"aoi_uploads_{run_id}"

    # Cleanup — test artefacts land in the repo root by default.
    saved_path.unlink(missing_ok=True)
    saved_path.parent.rmdir()


def test_process_bbox_rejects_untrusted_url(client):
    payload = {
        "aoi": {"kind": "url", "value": "https://evil.example.com/x.geojson"},
        "search_type": "overpasses",
    }
    resp = client.post("/process_bbox", json=payload)
    assert resp.status_code == 400
    assert (
        "Untrusted host" in resp.get_json()["error"]
        or "AOI" in resp.get_json()["error"]
    )


def test_process_bbox_rejects_missing_aoi(client):
    resp = client.post("/process_bbox", json={"search_type": "overpasses"})
    assert resp.status_code == 400
    assert (
        "bounding box" in resp.get_json()["error"].lower()
        or "aoi" in resp.get_json()["error"].lower()
    )


def test_processing_status_returns_stage_and_progress(client):
    run_id = web_app._create_run("disasters")
    web_app._set_progress(run_id, stage="Downloading granules", current=3, total=10)

    resp = client.get("/processing_status", query_string={"run_id": run_id})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["running"] is True
    assert body["stage"] == "Downloading granules"
    assert body["progress"] == {"current": 3, "total": 10}
    assert isinstance(body["elapsed_seconds"], (int, float))
    assert body["elapsed_seconds"] >= 0


def test_poll_dir_until_stopped_counts_files(tmp_path):
    run_id = web_app._create_run("disasters")
    # Prime a total so the poller only has to bump `current`.
    web_app._set_progress(run_id, current=0, total=3)

    (tmp_path / "OPERA_A.tif").write_bytes(b"a")
    (tmp_path / "OPERA_B.tif").write_bytes(b"b")
    (tmp_path / "OPERA_C.tif").write_bytes(b"c")
    (tmp_path / "unrelated.txt").write_bytes(b"skip")

    stop = threading.Event()
    thread = threading.Thread(
        target=web_app._poll_dir_until_stopped,
        args=(run_id, tmp_path, "OPERA_*.tif", stop, 0.05),
        daemon=True,
    )
    thread.start()
    # Let one poll cycle run.
    time.sleep(0.15)
    stop.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()

    state = web_app._get_run_state(run_id)
    assert state["progress"]["current"] == 3
    assert state["progress"]["total"] == 3


def test_aoi_preview_returns_geojson_for_geojson_upload(client):
    body = b'{"type":"Polygon","coordinates":[[[0,0],[2,0],[2,3],[0,3],[0,0]]]}'
    resp = client.post(
        "/aoi_preview",
        data={
            "payload_json": json.dumps({"aoi": {"kind": "file", "value": None}}),
            "aoi_file": (BytesIO(body), "preview.geojson"),
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    geom = resp.get_json()["geometry"]
    assert geom["type"] == "Polygon"
    # Round-trips the exterior ring; shapely serialises coords as tuples.
    assert geom["coordinates"][0][0] == [0, 0] or geom["coordinates"][0][0] == (0, 0)

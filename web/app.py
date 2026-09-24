import os

os.environ.setdefault("PANDAS_FUTURE_INFER_STRING", "0")

import glob
import html
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from disasters.pipeline import (
    PipelineConfig,
    run_download_only,
    run_mosaic_only,
    run_pipeline,
    run_search_only,
)
from flask import Flask, jsonify, render_template_string, request, send_from_directory

print("Flask is running with Python:", sys.executable)
logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MiB, covers AOI file uploads

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
HTML_FILE = "activated_events_map.html"
BASE_OUTPUT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Warm up the KML parser so the first .kml preview/upload doesn't pay a
# multi-second cold-import cost (GDAL/Fiona init on macOS runs ~2s), long
# enough that browsers or proxies sometimes abort the request.
try:
    from utils.utils import create_polygon_from_kml  # noqa: F401

    logging.info("Pre-loaded KML parser (utils.utils.create_polygon_from_kml)")
except Exception as _kml_import_exc:  # noqa: BLE001
    logging.warning("KML parser not available: %s", _kml_import_exc)

processing_runs = {}
processing_runs_lock = threading.Lock()


class _ThreadSafeSearchCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._data = {"signature": None, "folder": None}

    def __getitem__(self, key):
        with self._lock:
            return self._data[key]

    def __setitem__(self, key, value):
        with self._lock:
            self._data[key] = value

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def update(self, **kwargs):
        with self._lock:
            self._data.update(kwargs)

    def snapshot(self):
        with self._lock:
            return dict(self._data)


LAST_SEARCH_CACHE = _ThreadSafeSearchCache()

_ALLOWED_AOI_KINDS = {"draw", "coords", "wkt", "url", "file"}
_ALLOWED_UPLOAD_SUFFIXES = {".geojson", ".json", ".kml"}
_POINT_INFLATION_DEG = 0.01


class _AoiError(ValueError):
    """Raised for AOI parsing/validation problems. `status` is the HTTP code."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _parse_aoi_from_request(req):
    """Extract (payload, aoi, uploaded_file) from a Flask request.

    - JSON body: returns the parsed dict, either the payload's `aoi` field or a
      synthesised `{"kind": "coords", ...}` from top-level lat_min/… (backwards
      compatible with existing clients and tests).
    - Multipart body: reads `payload_json` field + `aoi_file` file part.

    Raises _AoiError on bad input.
    """
    ctype = (req.content_type or "").lower()

    uploaded = None
    if ctype.startswith("multipart/"):
        raw = req.form.get("payload_json")
        if not raw:
            raise _AoiError("multipart body missing payload_json field")
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise _AoiError(f"payload_json is not valid JSON: {exc}") from exc
        uploaded = req.files.get("aoi_file")
    elif ctype.startswith("application/json") or not ctype:
        payload = req.get_json(silent=True)
        if payload is None:
            raise _AoiError("Missing or invalid JSON body")
    else:
        raise _AoiError(f"Unsupported Content-Type: {ctype}", status=415)

    aoi = payload.get("aoi")
    if aoi is None:
        # Backwards compat: synthesise a coords AOI from top-level bbox keys.
        if all(k in payload for k in ("lat_min", "lat_max", "lon_min", "lon_max")):
            aoi = {
                "kind": "coords",
                "value": {
                    "lat_min": payload["lat_min"],
                    "lat_max": payload["lat_max"],
                    "lon_min": payload["lon_min"],
                    "lon_max": payload["lon_max"],
                },
            }
        else:
            raise _AoiError("Missing bounding box data")

    if not isinstance(aoi, dict) or aoi.get("kind") not in _ALLOWED_AOI_KINDS:
        raise _AoiError(f"Invalid aoi.kind (expected one of {_ALLOWED_AOI_KINDS})")

    kind = aoi["kind"]
    value = aoi.get("value")
    if kind in ("draw", "coords"):
        if not isinstance(value, dict) or not all(
            isinstance(value.get(k), (int, float))
            for k in ("lat_min", "lat_max", "lon_min", "lon_max")
        ):
            raise _AoiError("draw/coords AOI needs 4 numeric bbox fields")
    elif kind == "wkt":
        if not isinstance(value, str) or not value.strip().upper().startswith(
            ("POINT", "POLYGON")
        ):
            raise _AoiError("WKT AOI must start with POINT or POLYGON")
    elif kind == "url":
        if not isinstance(value, str) or not value.strip().lower().startswith(
            "https://"
        ):
            raise _AoiError("URL AOI must be an https URL")
    elif kind == "file":
        if uploaded is None or not uploaded.filename:
            raise _AoiError("file AOI requires an uploaded aoi_file part")

    return payload, aoi, uploaded


def _aoi_to_bbox_and_geometry(aoi, run_dir):
    """Reduce any AOI kind to (bbox4, shapely_geometry).

    bbox4 = [lat_min, lat_max, lon_min, lon_max]. Zero-width or zero-height
    boxes (POINT input or coincident corners) are inflated by ±_POINT_INFLATION_DEG
    so the downstream disasters pipeline doesn't reject a zero-area rectangle.
    The returned geometry is NOT inflated — that stays exact for next_pass.
    """
    # Local import to keep module load light and match the pattern in
    # _bbox_to_geometry (which also imports shapely lazily).
    from shapely.geometry import box

    from disaster_alerts.plot_html_map import _bbox_to_geometry

    kind = aoi["kind"]
    if kind in ("draw", "coords"):
        v = aoi["value"]
        geom = box(v["lon_min"], v["lat_min"], v["lon_max"], v["lat_max"])
    elif kind == "wkt":
        geom, _, _ = _bbox_to_geometry(aoi["value"], run_dir)
    elif kind == "url":
        geom, _, _ = _bbox_to_geometry(aoi["value"], run_dir)
    elif kind == "file":
        from disaster_alerts.plot_html_map import _geometry_from_file

        geom = _geometry_from_file(aoi["value"])
    else:
        raise _AoiError(f"Unknown aoi.kind={kind}")

    lon_min, lat_min, lon_max, lat_max = geom.bounds
    if lat_max == lat_min:
        lat_min -= _POINT_INFLATION_DEG
        lat_max += _POINT_INFLATION_DEG
        logging.warning(
            "AOI collapsed on latitude; inflated by ±%s°", _POINT_INFLATION_DEG
        )
    if lon_max == lon_min:
        lon_min -= _POINT_INFLATION_DEG
        lon_max += _POINT_INFLATION_DEG
        logging.warning(
            "AOI collapsed on longitude; inflated by ±%s°", _POINT_INFLATION_DEG
        )

    return [lat_min, lat_max, lon_min, lon_max], geom


def _get_search_signature(params):
    import json

    prods = params.get("products") or []
    sats = params.get("satellites") or []

    sig_dict = {
        "bbox": [
            params.get("lat_min"),
            params.get("lat_max"),
            params.get("lon_min"),
            params.get("lon_max"),
        ],
        "products": sorted(prods),
        "satellites": sorted(sats),
        "functionality": params.get("functionality", "opera_search"),
        "date_strat": params.get("dis_date_strat"),
        "recent_n": params.get("dis_recent_n"),
        "single_date": params.get("dis_single_date"),
        "start_date": params.get("dis_start_date"),
        "end_date": params.get("dis_end_date"),
        "opt_cloud": bool(params.get("opt_cloud", False)),
    }

    return json.dumps(sig_dict, sort_keys=True)


def _list_output_folders():
    return sorted(
        glob.glob(os.path.join(BASE_OUTPUT_DIR, "nextpass_outputs_*")),
        key=os.path.getmtime,
        reverse=True,
    )


def _create_run(search_type, task_count=1):
    run_id = uuid.uuid4().hex
    run_state = {
        "running": True,
        "active_tasks": task_count,
        "latest_folder": None,
        "error": None,
        "search_type": search_type or ["opera_search"],
        "started_at": time.time(),
        # Live progress fields — populated by workers via _set_progress.
        # `stop_poll` is a threading.Event used to signal any active
        # disk-polling thread to shut down; not JSON-serialised.
        "stage": None,
        "progress": None,
        "stop_poll": threading.Event(),
    }
    with processing_runs_lock:
        processing_runs[run_id] = run_state
    return run_id


def _set_progress(run_id, *, stage=None, current=None, total=None):
    """Publish stage/progress updates onto a run's state.

    Passing only `stage` leaves the numeric progress unchanged. Passing
    `current` and/or `total` merges them into the existing progress dict
    (creating one if needed), so a caller can bump `current` without
    knowing `total` and vice versa.
    """
    updates = {}
    if stage is not None:
        updates["stage"] = stage
    if current is not None or total is not None:
        with processing_runs_lock:
            existing = (processing_runs.get(run_id) or {}).get("progress") or {}
        updates["progress"] = {
            "current": current if current is not None else existing.get("current", 0),
            "total": total if total is not None else existing.get("total", 0),
        }
    if updates:
        _update_run_state(run_id, **updates)


def _poll_dir_until_stopped(run_id, watch_dir, pattern, stop_event, interval=0.5):
    """Push a live count of files matching `pattern` in `watch_dir` into
    run_state['progress']['current'] until `stop_event` is set. Runs in a
    dedicated daemon thread.
    """
    watch_dir = Path(watch_dir)
    while not stop_event.wait(interval):
        try:
            n = sum(1 for _ in watch_dir.glob(pattern))
        except OSError:
            n = 0
        _set_progress(run_id, current=n)


def _monitor_disasters_progress(
    run_id,
    data_dir,
    mosaic_dir,
    expected_downloads,
    expected_mosaics,
    stop_event,
    interval=0.5,
):
    """Watch both download and mosaic output directories concurrently and
    publish the current stage + count. When the first *_mosaic.tif appears
    the reported stage switches from download to mosaic — a heuristic that
    matches how run_pipeline sequences its work.
    """
    data_dir = Path(data_dir)
    mosaic_dir = Path(mosaic_dir)
    while not stop_event.wait(interval):
        try:
            n_downloads = sum(1 for _ in data_dir.glob("OPERA_*.tif"))
        except OSError:
            n_downloads = 0
        try:
            n_mosaics = sum(1 for _ in mosaic_dir.glob("*_mosaic.tif"))
        except OSError:
            n_mosaics = 0
        if n_mosaics > 0 and expected_mosaics > 0:
            _set_progress(
                run_id,
                stage="Mosaicking products",
                current=n_mosaics,
                total=expected_mosaics,
            )
        else:
            _set_progress(
                run_id,
                stage="Downloading granules",
                current=n_downloads,
                total=expected_downloads,
            )


def _watch_for_catalog_phase(run_id, run_start_time, stop_event, interval=0.5):
    """Detect the overpasses → OPERA-catalog transition inside a
    functionality="both" next_pass run.

    next_pass.main() (next_pass.py:375) creates a `nextpass_outputs_<ts>/`
    folder relative to CWD (== BASE_OUTPUT_DIR for the Flask process), and
    writes `satellite_overpasses_map.html` inside it at the end of the
    overpasses phase — immediately before the catalog query starts. We
    watch for that file with an mtime filter to ignore stale artefacts
    from earlier runs.
    """
    base = Path(BASE_OUTPUT_DIR)
    while not stop_event.wait(interval):
        try:
            for nxt in base.glob("nextpass_outputs_*"):
                marker = nxt / "satellite_overpasses_map.html"
                try:
                    if marker.is_file() and marker.stat().st_mtime >= run_start_time:
                        _set_progress(run_id, stage="Searching OPERA catalog")
                        return
                except OSError:
                    continue
        except OSError:
            continue


def _estimate_disasters_totals(search_dir, target_products):
    """Read search metadata to estimate expected download and mosaic counts.

    Returns (expected_downloads, expected_mosaics). Zero on either means
    'unknown' — the client renders an indeterminate bar in that case.
    """
    try:
        from disasters.pipeline import read_opera_metadata

        df = read_opera_metadata(search_dir)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Progress estimator: failed to read metadata: %s", exc)
        return 0, 0

    if df.empty or "Dataset" not in df.columns:
        return 0, 0

    targets = target_products or df["Dataset"].dropna().unique().tolist()
    sub = df[df["Dataset"].isin(targets)]
    url_cols = [c for c in df.columns if c.startswith("Download URL")]
    if not url_cols:
        return 0, 0

    expected_downloads = int(sub[url_cols].notna().sum().sum())

    # Rough mosaic count: one mosaic per (dataset × unique date × layer).
    # Off by a few due to time clustering, but far better than a spinner.
    date_col = "Start Date" if "Start Date" in sub.columns else "Start Time"
    n_dates = sub[date_col].dropna().nunique() if date_col in sub.columns else 1
    expected_mosaics = len(targets) * max(1, n_dates) * len(url_cols)
    return expected_downloads, expected_mosaics


def _get_run_state(run_id):
    if not run_id:
        return None
    with processing_runs_lock:
        run_state = processing_runs.get(run_id)
        if run_state is None:
            return None
        return dict(run_state)


def _update_run_state(run_id, **updates):
    with processing_runs_lock:
        run_state = processing_runs.get(run_id)
        if run_state is None:
            return
        run_state.update(updates)


def _mark_task_complete(run_id):
    """Safely decrements the active task counter and marks run false when 0."""
    run_finished = False
    with processing_runs_lock:
        run_state = processing_runs.get(run_id)
        if run_state is None:
            return
        run_state["active_tasks"] -= 1
        if run_state["active_tasks"] <= 0:
            run_state["running"] = False
            run_finished = True

    if run_finished:
        aoi_dir = Path(BASE_OUTPUT_DIR) / f"aoi_uploads_{run_id}"
        if aoi_dir.is_dir():
            shutil.rmtree(aoi_dir, ignore_errors=True)


def generate_web_png(tif_path, png_path):
    """Converts a local GeoTIFF to a transparent web-ready PNG
    using embedded colormaps when available, otherwise applying a sequential Reds scheme.
    """

    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.warp import transform_bounds

    with rasterio.open(tif_path) as src:
        # Convert native projection coordinates to global Lat/Lng for Leaflet
        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        leaflet_bounds = [[bounds[1], bounds[0]], [bounds[3], bounds[2]]]

        # Read data
        data = src.read(1)
        h, w = data.shape
        rgba = np.zeros((h, w, 4), dtype=np.uint8)

        # Extract nodata value
        nodata_val = src.nodata if src.nodata is not None else 255
        nodata_mask = (data == nodata_val) | (data == 255) | (data == 0)

        try:
            cmap = src.colormap(1)
        except ValueError:
            cmap = None

        # If present, apply the embedded color table
        if cmap:
            for pixel_value, rgba_color in cmap.items():
                rgba[data == pixel_value] = rgba_color

        # If no embedded color table, apply 'Reds' sequential colormap
        else:
            valid_pixels = data[~nodata_mask]
            if valid_pixels.size > 0:
                d_min, d_max = int(valid_pixels.min()), int(valid_pixels.max())

                if d_max > d_min:
                    # Cast to float transiently for color gradient ratio calculation
                    norm = (data.astype(np.float32) - d_min) / (d_max - d_min)
                    norm = np.clip(norm, 0.0, 1.0)
                else:
                    norm = np.zeros_like(data, dtype=np.float32)

                # Sequential Reds Curve: High integer anomaly values yield stark Red
                rgba[..., 0] = 255  # Max Red
                rgba[..., 1] = (255 * (1.0 - norm * 0.85)).astype(
                    np.uint8
                )  # Green Channel
                rgba[..., 2] = (255 * (1.0 - norm * 0.85)).astype(
                    np.uint8
                )  # Blue Channel
                rgba[..., 3] = 216  # Clean baseline opacity
            else:
                rgba[..., 3] = 0

        # Enforce transparency for nodata pixels
        rgba[nodata_mask] = [0, 0, 0, 0]

        # Compress and save out the web asset
        img = Image.fromarray(rgba, "RGBA")
        img.save(png_path, "PNG")

        return leaflet_bounds


def run_overpasses_only(run_id, params):
    """
    Builds the command line arguments based on the dashboard panel selections.
    Executes a next_pass query for satellite overpasses ONLY.
    """
    run_state = _get_run_state(run_id)
    if run_state is None:
        return

    stage_stop = None
    stage_monitor = None
    try:
        _set_progress(run_id, stage="Retrieving overpasses")
        # DRCS map generation is gated inside next_pass on
        # functionality == "both" (see next_pass.py:436 — it needs OPERA
        # granule results to overlay against the overpass grid). Auto-
        # upgrade the -f flag when the user enabled DRCS so their
        # request isn't silently dropped.
        drcs_enabled = bool(params.get("drcs") == "yes" and params.get("event_date"))
        functionality_arg = "both" if drcs_enabled else "overpasses"
        # Rich AOI form (WKT / URL / file path / 4 floats) is stashed on the
        # run state by /process_bbox so next_pass gets the exact input the
        # user provided, not the axis-aligned reduction.
        cmd = [
            sys.executable,
            "-m",
            "next_pass",
            "-b",
            *run_state["np_bbox_arg"],
            "-f",
            functionality_arg,
        ]

        # Append UI parameters specific to the "Next Pass" panel
        if params.get("satellites") and "all" not in params["satellites"]:
            cmd.extend(["-s"] + params["satellites"])

        if params.get("np_lookback") and str(params["np_lookback"]).isdigit():
            cmd.extend(["-k", str(params["np_lookback"])])

        if drcs_enabled:
            cmd.extend(["-g", params["event_date"]])
            # With functionality="both" next_pass runs overpasses then the
            # OPERA catalog before writing the DRCS map. Show the same
            # two-stage progress as run_opera_search / run_disasters.
            stage_stop = threading.Event()
            _update_run_state(run_id, stop_poll=stage_stop)
            stage_monitor = threading.Thread(
                target=_watch_for_catalog_phase,
                args=(run_id, run_state["started_at"], stage_stop),
                daemon=True,
            )
            stage_monitor.start()

        # Take a snapshot of folders before running, execute,
        # then find the newly created folder
        before_folders = set(
            glob.glob(os.path.join(BASE_OUTPUT_DIR, "nextpass_outputs_*"))
        )
        subprocess.run(cmd, check=True, cwd=BASE_OUTPUT_DIR)
        after_folders = set(
            glob.glob(os.path.join(BASE_OUTPUT_DIR, "nextpass_outputs_*"))
        )

        new_folders = list(after_folders - before_folders)
        if new_folders:
            _update_run_state(
                run_id, latest_folder=max(new_folders, key=os.path.getmtime)
            )
        else:
            _update_run_state(run_id, error="No output folder could be matched.")
    except Exception as e:
        _update_run_state(run_id, error=str(e))
    finally:
        if stage_stop is not None:
            stage_stop.set()
            if stage_monitor is not None:
                stage_monitor.join(timeout=1)
        _mark_task_complete(run_id)


def run_opera_search(run_id, params):
    """
    Queries the Earthdata catalog for OPERA products using the 'disasters' pipeline.
    Caches the resulting output directory for faster downstream mosaicking.
    """
    run_state = _get_run_state(run_id)
    if run_state is None:
        return

    try:
        bbox = [
            float(params["lat_min"]),
            float(params["lat_max"]),
            float(params["lon_min"]),
            float(params["lon_max"]),
        ]

        # Parse product selections directly from the UI
        raw_products = params.get("products", [])
        target_products = [p for p in raw_products if p != "all"]

        # If "all" was chosen or nothing was selected,
        # pass None to search the full catalog
        search_products = (
            None if (not target_products or "all" in raw_products) else target_products
        )

        # Even though this is purely a search, map the
        # advanced Disasters date panel logic
        date_strat = params.get("dis_date_strat", "range")
        pipeline_date = None
        number_of_dates = 5

        if date_strat == "single":
            pipeline_date = params.get("dis_single_date")
        elif date_strat == "range":
            if params.get("dis_start_date") and params.get("dis_end_date"):
                pipeline_date = f"{params['dis_start_date']}/{params['dis_end_date']}"
        elif date_strat == "recent":
            r_val = params.get("dis_recent_n")
            if isinstance(r_val, str):
                r_val = r_val.strip()
            number_of_dates = int(r_val) if r_val and str(r_val).isdigit() else 5

        # Isolate the search output
        output_dir = Path(BASE_OUTPUT_DIR) / f"search_outputs_{run_id}"

        # With functionality="both" run_search_only does overpasses first
        # (inside next_pass) then the OPERA catalog query. Start with the
        # overpasses label and swap once next_pass writes its overpasses
        # HTML into BASE_OUTPUT_DIR/nextpass_outputs_<ts>/.
        functionality = params.get("functionality", "opera_search")
        stage_monitor = None
        stage_stop = None
        if functionality == "both":
            _set_progress(run_id, stage="Retrieving overpasses")
            stage_stop = threading.Event()
            _update_run_state(run_id, stop_poll=stage_stop)
            stage_monitor = threading.Thread(
                target=_watch_for_catalog_phase,
                args=(run_id, run_state["started_at"], stage_stop),
                daemon=True,
            )
            stage_monitor.start()
        else:
            _set_progress(run_id, stage="Searching OPERA catalog")

        try:
            # Execute the search natively in Python
            result_dir = run_search_only(
                bbox=bbox,
                output_dir=output_dir,
                product=search_products,
                date=pipeline_date,
                number_of_dates=number_of_dates,
                functionality=functionality,
                compute_cloudiness=bool(params.get("opt_cloud", False)),
                satellites=(
                    params.get("satellites")
                    if "all" not in params.get("satellites", [])
                    else None
                ),
            )
        finally:
            if stage_stop is not None:
                stage_stop.set()
                stage_monitor.join(timeout=1)

        # Cache the search signature and folder path
        if result_dir:
            LAST_SEARCH_CACHE.update(
                signature=_get_search_signature(params), folder=result_dir
            )
            _update_run_state(run_id, latest_folder=str(result_dir))
        else:
            _update_run_state(run_id, error="Search exited gracefully without outputs.")

    except Exception as e:
        _update_run_state(run_id, error=str(e))
    finally:
        _mark_task_complete(run_id)


def run_disasters(run_id, params):
    """
    Runs the full end-to-end disasters pipeline.
    Phase 1: Generates a unified HTML map of all products.
    Phase 2: Loops through each product to mosaic individually.
    """
    run_state = _get_run_state(run_id)
    if run_state is None:
        return

    try:
        bbox = [
            float(params["lat_min"]),
            float(params["lat_max"]),
            float(params["lon_min"]),
            float(params["lon_max"]),
        ]

        # Parse product selections directly from the UI
        raw_products = params.get("products", [])
        target_products = [p for p in raw_products if p != "all"]

        # Phase 1 needs None for a unified comprehensive search if "all" is active
        search_products = (
            None if (not target_products or "all" in raw_products) else target_products
        )

        # Parse the Disasters Date panel logic
        date_strat = params.get("dis_date_strat", "range")
        pipeline_date = None
        number_of_dates = 5

        if date_strat == "single":
            pipeline_date = params.get("dis_single_date")
            if not pipeline_date:
                raise ValueError("dis_single_date is required for single-date mode")
        elif date_strat == "range":
            start_date = params.get("dis_start_date")
            end_date = params.get("dis_end_date")
            if not start_date or not end_date:
                raise ValueError(
                    "dis_start_date and dis_end_date are required for range mode"
                )
            pipeline_date = f"{start_date}/{end_date}"
        elif date_strat == "recent":
            r_val = params.get("dis_recent_n")
            if isinstance(r_val, str):
                r_val = r_val.strip()
            number_of_dates = int(r_val) if r_val and str(r_val).isdigit() else 5
        else:
            raise ValueError(f"Unsupported dis_date_strat: {date_strat}")

        output_dir = Path(BASE_OUTPUT_DIR) / f"disasters_outputs_{run_id}"
        output_dir.mkdir(parents=True, exist_ok=True)
        _update_run_state(run_id, latest_folder=str(output_dir))

        action = params.get("dis_action", "run")
        search_type = params.get("search_type", ["opera_search"])
        functionality = (
            "both"
            if "overpasses" in search_type or "all" in search_type
            else "opera_search"
        )
        satellites_list = (
            params.get("satellites")
            if "all" not in params.get("satellites", [])
            else None
        )

        # =========================================================
        # PHASE 1: UNIFIED SEARCH (Generates 1 Map for All Layers)
        # =========================================================
        current_sig = _get_search_signature(params)
        search_dir = None

        # Take a frozen snapshot to prevent race conditions (From main)
        cache_snap = LAST_SEARCH_CACHE.snapshot()

        if cache_snap["signature"] == current_sig and cache_snap["folder"]:
            search_dir = Path(cache_snap["folder"])
        else:
            # functionality="both" triggers overpasses inside run_search_only
            # before the catalog query — split the label accordingly.
            stage_stop = None
            stage_monitor = None
            if functionality == "both":
                _set_progress(run_id, stage="Retrieving overpasses")
                stage_stop = threading.Event()
                stage_monitor = threading.Thread(
                    target=_watch_for_catalog_phase,
                    args=(run_id, run_state["started_at"], stage_stop),
                    daemon=True,
                )
                stage_monitor.start()
            else:
                _set_progress(run_id, stage="Searching OPERA catalog")
            try:
                search_dir = run_search_only(
                    bbox=bbox,
                    output_dir=output_dir,
                    product=search_products,
                    date=pipeline_date,
                    number_of_dates=number_of_dates,
                    functionality=functionality,
                    compute_cloudiness=bool(params.get("opt_cloud", False)),
                    satellites=satellites_list,
                )
            finally:
                if stage_stop is not None:
                    stage_stop.set()
                    stage_monitor.join(timeout=1)
            if search_dir:
                LAST_SEARCH_CACHE.update(signature=current_sig, folder=search_dir)

        # =========================================================
        # PHASE 2: PROCESSING (Mosaics Individually)
        # =========================================================
        if not target_products and search_dir:
            from disasters.pipeline import read_opera_metadata

            try:
                df_found = read_opera_metadata(search_dir)
                if not df_found.empty and "Dataset" in df_found.columns:
                    target_products = df_found["Dataset"].dropna().unique().tolist()
            except Exception as e:
                logging.warning(f"Failed to dynamically read catalog metadata: {e}")

        if not target_products:
            target_products = ["OPERA_L3_DSWX-HLS_V1"]

        # Estimate work sizes from the search metadata so the overlay can
        # show real N/M counts instead of an indeterminate spinner.
        expected_downloads, expected_mosaics = _estimate_disasters_totals(
            search_dir, target_products
        )
        stop_event = run_state.get("stop_poll") or threading.Event()
        _update_run_state(run_id, stop_poll=stop_event)
        _set_progress(
            run_id,
            stage="Downloading granules",
            current=0,
            total=expected_downloads,
        )
        monitor = threading.Thread(
            target=_monitor_disasters_progress,
            args=(
                run_id,
                Path(output_dir) / "data",
                Path(output_dir),
                expected_downloads,
                expected_mosaics,
                stop_event,
            ),
            daemon=True,
        )
        monitor.start()

        # Pass the entire list to the newly upgraded pipeline functions
        mode_dir = None
        try:
            if action == "download":
                res_dir = run_download_only(
                    bbox=bbox,
                    output_dir=output_dir,
                    date=pipeline_date,
                    number_of_dates=number_of_dates,
                    product=target_products,
                    functionality=functionality,
                    compute_cloudiness=bool(params.get("opt_cloud", False)),
                )
                if res_dir:
                    mode_dir = res_dir

            elif action == "mosaic":
                data_dir = run_download_only(
                    bbox=bbox,
                    output_dir=output_dir,
                    date=pipeline_date,
                    number_of_dates=number_of_dates,
                    product=target_products,
                    functionality=functionality,
                    compute_cloudiness=bool(params.get("opt_cloud", False)),
                )
                if data_dir:
                    res_dir = run_mosaic_only(
                        input_dir=data_dir,
                        output_dir=output_dir,
                        bbox=bbox,
                        benchmark=False,
                    )
                    if res_dir:
                        mode_dir = res_dir

            else:
                config = PipelineConfig(
                    bbox=bbox,
                    output_dir=output_dir,
                    local_dir=None,
                    search_dir=search_dir,
                    product=target_products,
                    functionality=functionality,
                    satellites=satellites_list,
                    date=pipeline_date,
                    number_of_dates=number_of_dates,
                    layout_title=(
                        f"Disaster Analysis ({bbox[0]:.2f},{bbox[2]:.2f} –"
                        f" {bbox[1]:.2f},{bbox[3]:.2f})"
                    ),
                    reclassify_snow_ice=bool(params.get("opt_rc", False)),
                    compute_cloudiness=bool(params.get("opt_cloud", False)),
                    no_mask=bool(params.get("opt_nomask", False)),
                    filter_date=params.get("opt_fd") or None,
                    slope_threshold=(
                        int(params["opt_st"])
                        if str(params.get("opt_st")).isdigit()
                        else None
                    ),
                )
                res_dir = run_pipeline(config)
                if res_dir:
                    mode_dir = res_dir
        finally:
            stop_event.set()
            monitor.join(timeout=1)

        if mode_dir and mode_dir.exists():
            _update_run_state(run_id, latest_folder=str(output_dir))
        else:
            _update_run_state(
                run_id, error="Processing exited without generating a mode folder."
            )

    except Exception as e:
        _update_run_state(run_id, error=str(e))
    finally:
        _mark_task_complete(run_id)


# ---- Serve original map ----
@app.route("/")
def index():
    return send_from_directory(DATA_DIR, HTML_FILE)


# ---- Ping endpoint ----
@app.route("/test_ping", methods=["GET"])
def test_ping():
    print("Ping received!")
    return "pong", 200


# ---- AOI preview (for URL and KML client-side rendering) ----
@app.route("/aoi_preview", methods=["POST"])
def aoi_preview():
    """Return the GeoJSON of a proposed AOI so the browser can render it
    before SEARCH. Used by URL and KML modes, which the browser can't preview
    itself (CORS for URL, no built-in parser for KML).
    """
    import tempfile

    from werkzeug.utils import secure_filename

    try:
        _payload, aoi, uploaded = _parse_aoi_from_request(request)
    except _AoiError as exc:
        return jsonify({"error": str(exc)}), exc.status

    with tempfile.TemporaryDirectory(
        prefix="aoi_preview_", dir=BASE_OUTPUT_DIR
    ) as tmpdir:
        tmp_path = Path(tmpdir)

        if aoi["kind"] == "file":
            safe = secure_filename(uploaded.filename) or "aoi_preview_upload"
            suffix = Path(safe).suffix.lower()
            if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
                return (
                    jsonify({"error": f"Unsupported upload suffix {suffix!r}"}),
                    400,
                )
            saved = tmp_path / safe
            uploaded.save(str(saved))
            aoi["value"] = str(saved)

        try:
            _bbox, geom = _aoi_to_bbox_and_geometry(aoi, tmp_path)
        except _AoiError as exc:
            return jsonify({"error": str(exc)}), exc.status
        except Exception:  # noqa: BLE001 - log details, keep response generic
            logging.exception("AOI preview failed")
            return jsonify({"error": "Failed to parse AOI input"}), 400

        payload = {"geometry": geom.__geo_interface__}

    return jsonify(payload)


# ---- Process bbox ----
@app.route("/process_bbox", methods=["POST"])
def process_bbox():
    from werkzeug.utils import secure_filename

    try:
        data, aoi, uploaded = _parse_aoi_from_request(request)
    except _AoiError as exc:
        return jsonify({"error": str(exc)}), exc.status

    search_type = data.get("search_type", ["opera_search"])
    if isinstance(search_type, str):
        search_type = [search_type]

    # DRCS map generation only exists on the next_pass CLI (next_pass.py:436
    # gates it on functionality="both", and next_pass.run_next_pass() — the
    # Python API used by disasters.pipeline.run_search_only — doesn't expose
    # the -g knob at all). So any DRCS-enabled request has to route through
    # run_overpasses_only (which invokes the CLI subprocess and already
    # auto-upgrades -f to "both"). This overrides the workflow-tick routing.
    drcs_wanted = bool(data.get("drcs") == "yes" and data.get("event_date"))

    targets = []
    if drcs_wanted and (
        "overpasses" in search_type
        or "opera_search" in search_type
        or "all" in search_type
    ):
        targets.append(run_overpasses_only)
    elif "disasters" in search_type or "all" in search_type:
        targets.append(run_disasters)
    elif "opera_search" in search_type and "overpasses" in search_type:
        data["functionality"] = "both"
        targets.append(run_opera_search)
    elif "opera_search" in search_type:
        data["functionality"] = "opera_search"
        targets.append(run_opera_search)
    elif "overpasses" in search_type:
        targets.append(run_overpasses_only)

    if not targets:
        return jsonify({"error": "No valid workflows selected"}), 400

    run_id = _create_run(search_type, task_count=len(targets))

    # Prepare a per-run scratch dir for uploads/URL downloads.
    run_dir = Path(BASE_OUTPUT_DIR) / f"aoi_uploads_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)

    if aoi["kind"] == "file":
        safe_name = secure_filename(uploaded.filename) or "aoi_upload"
        suffix = Path(safe_name).suffix.lower()
        if suffix not in _ALLOWED_UPLOAD_SUFFIXES:
            return (
                jsonify({"error": f"Unsupported upload suffix {suffix!r}"}),
                400,
            )
        saved_path = run_dir / safe_name
        uploaded.save(str(saved_path))
        aoi["value"] = str(saved_path)

    try:
        bbox, _geom = _aoi_to_bbox_and_geometry(aoi, run_dir)
    except _AoiError as exc:
        return jsonify({"error": str(exc)}), exc.status
    except Exception:  # noqa: BLE001 - log details, keep response generic
        logging.exception("AOI parse failure")
        return jsonify({"error": "Failed to parse AOI input"}), 400

    # The client's own numbers are left alone when they're usable: keeping the
    # params dict byte-identical for draw/coords preserves backwards
    # compatibility with existing /process_bbox JSON callers. A box that
    # collapses on either axis is not usable — run_disasters feeds these keys
    # into the mosaic master grid, where zero extent yields a 1x1 pixel
    # product — so there the inflated bbox wins. The map's point tool and
    # coincident typed corners both send exactly that.
    drawn = aoi["value"] if aoi["kind"] in ("draw", "coords") else None
    zero_extent = drawn is not None and (
        drawn["lat_min"] == drawn["lat_max"] or drawn["lon_min"] == drawn["lon_max"]
    )
    if zero_extent or not all(
        k in data for k in ("lat_min", "lat_max", "lon_min", "lon_max")
    ):
        data["lat_min"], data["lat_max"], data["lon_min"], data["lon_max"] = bbox

    # Compute next_pass -b tokens inline (per trim decision, no helper).
    if drawn is not None:
        if (
            drawn["lat_min"] == drawn["lat_max"]
            and drawn["lon_min"] == drawn["lon_max"]
        ):
            # next_pass reads two tokens as an exact point (utils.bbox_type),
            # so a point AOI stays a point instead of the inflated box.
            np_bbox_arg = [str(drawn["lat_min"]), str(drawn["lon_min"])]
        else:
            np_bbox_arg = [str(bbox[0]), str(bbox[1]), str(bbox[2]), str(bbox[3])]
    elif aoi["kind"] == "wkt":
        np_bbox_arg = [aoi["value"]]
    elif aoi["kind"] == "url":
        np_bbox_arg = [aoi["value"]]
    else:  # file
        np_bbox_arg = [aoi["value"]]

    # Stash next_pass tokens in run_state so run_overpasses_only can read
    # them WITHOUT polluting the params dict (which existing tests assert on).
    _update_run_state(run_id, np_bbox_arg=np_bbox_arg)

    print(
        f"Received search request for bbox: {data.get('lat_min')},"
        f" {data.get('lon_min')}"
    )

    for target in targets:
        threading.Thread(target=target, args=(run_id, data), daemon=True).start()

    return jsonify({"status": "processing started", "run_id": run_id})


# ---- Status endpoint ----
@app.route("/processing_status")
def processing_status():
    run_id = request.args.get("run_id")
    run_state = _get_run_state(run_id)
    if run_state is None:
        return jsonify({"error": "Unknown or missing run_id"}), 404

    started_at = run_state.get("started_at") or time.time()
    return jsonify(
        {
            "running": run_state["running"],
            "error": run_state["error"],
            "stage": run_state.get("stage"),
            "progress": run_state.get("progress"),
            "elapsed_seconds": max(0.0, time.time() - started_at),
        }
    )


# ---- Serve maps from latest next-pass or disasters folder ----
@app.route("/maps/<run_id>/<path:filename>")
def maps(run_id, filename):
    run_state = _get_run_state(run_id)
    if run_state is None:
        return f"Unknown run: {run_id}", 404

    folder = run_state.get("latest_folder")
    if not folder:
        return f"No folder matched for run: {run_id}", 404

    file_path = os.path.join(folder, filename)

    if os.path.exists(file_path):
        directory = os.path.dirname(file_path)
        name = os.path.basename(file_path)
        return send_from_directory(directory, name)

    return f"File {filename} not found", 404


@app.route("/show_maps")
def show_maps():
    """
    Display run_output.txt first, then the maps below it.
    Renders web-optimized PNG overlays using embedded colormaps for Disasters.
    """
    run_id = request.args.get("run_id")
    run_state = _get_run_state(run_id)
    if run_state is None:
        return "<h3>No run selected. Start a dashboard search first.</h3>", 404

    folder = run_state.get("latest_folder")
    if run_state.get("running") and not folder:
        return (
            "<h3>This search is still processing. Please wait a moment and try"
            " again.</h3>"
        )

    if run_state.get("error"):
        return f"<h3>Processing failed: {html.escape(run_state['error'])}</h3>", 500

    if not folder:
        return (
            "<h3>No output found for this search.</h3>",
            404,
        )

    log_file = os.path.join(folder, "run_output.txt")
    log_content = ""
    if os.path.exists(log_file):
        with open(log_file, "r") as f:
            log_content = html.escape(f.read())

    log_html = f"<pre>{log_content}</pre>" if log_content.strip() else ""

    search_type = run_state.get("search_type", ["opera_search"])
    if isinstance(search_type, str):
        search_type = [search_type]

    uses_disasters = "disasters" in search_type or "all" in search_type

    sat_map_path = next(Path(folder).rglob("satellite_overpasses_map.html"), None)
    opera_map_path = next(Path(folder).rglob("opera_products_map.html"), None)
    drcs_map_path = next(Path(folder).rglob("opera_products_drcs_map.html"), None)

    show_sat = sat_map_path is not None
    show_opera = opera_map_path is not None
    show_drcs = drcs_map_path is not None

    iframes = ""
    if show_sat:
        rel_sat = os.path.relpath(sat_map_path, folder).replace(os.sep, "/")
        iframes += f'<iframe src="/maps/{run_id}/{rel_sat}"></iframe>'
    if show_opera:
        rel_opera = os.path.relpath(opera_map_path, folder).replace(os.sep, "/")
        iframes += f'<iframe src="/maps/{run_id}/{rel_opera}"></iframe>'
    if show_drcs:
        rel_drcs = os.path.relpath(drcs_map_path, folder).replace(os.sep, "/")
        iframes += f'<iframe src="/maps/{run_id}/{rel_drcs}"></iframe>'

    # Loop through and convert TIF files to PNG on-the-fly
    geotiff_viewer = ""
    tif_layers_json = []

    if uses_disasters:
        for root, _, files in os.walk(folder):
            for file in files:
                if (
                    file.endswith(".tif")
                    and "RTC" not in file
                    and not file.startswith(("tmp_", "."))
                ):
                    full_tif_path = os.path.join(root, file)
                    full_png_path = full_tif_path.replace(".tif", ".png")

                    try:
                        img_bounds = generate_web_png(full_tif_path, full_png_path)

                        rel_path = os.path.relpath(full_png_path, folder)
                        url_path = rel_path.replace(os.sep, "/")
                        url = f"/maps/{run_id}/{url_path}"

                        display_name = (
                            file.replace("_mosaic", "")
                            .replace(".tif", "")
                            .replace("OPERA_L3_", "")
                            .replace("OPERA_L2_", "")
                        )

                        date_matches = re.findall(r"\d{8}T\d+[A-Za-z]*", display_name)
                        if date_matches:
                            for d in date_matches:
                                date_str = d[:8]
                                time_str = d[9:13]
                                formatted_time = (
                                    f"{time_str[:2]}:{time_str[2:]}"
                                    if len(time_str) == 4
                                    else time_str
                                )
                                f_date = (
                                    f" [{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                                    f" {formatted_time}] "
                                )
                                display_name = display_name.replace(d, f_date)

                        display_name = display_name.replace("_", " ").strip()
                        display_name = re.sub(r"\s+", " ", display_name)

                        tif_layers_json.append(
                            {"url": url, "name": display_name, "bounds": img_bounds}
                        )
                    except Exception as e:
                        logging.error(f"Failed converting {file} to web PNG: {e}")

        # If any valid GeoTIFFs, render the Leaflet viewer
        if tif_layers_json:
            geotiff_viewer = f"""
            <div id="geotiff-map" style="flex: 1; height: 100%;"></div>
            <link rel="stylesheet"
                  href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script>
                var streetMap = L.tileLayer(
                    'https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png',
                    {{
                        attribution:
                            '&copy; <a href="https://www.openstreetmap.org/'
                            + 'copyright">OpenStreetMap</a> contributors '
                            + '&copy; <a href="https://carto.com/'
                            + 'attributions">CARTO</a>',
                        maxZoom: 20
                    }}
                );
                var satellite = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{x}}/{{y}}.png');

                var map = L.map('geotiff-map', {{
                    center: [0, 0],
                    zoom: 2,
                    layers: [satellite]
                }});

                var baseMaps = {{ "Satellite": satellite, "Street Map": streetMap }};
                var overlayMaps = {{}};

                var layerControl = L.control.layers(
                    baseMaps, overlayMaps, {{ collapsed: true }}
                ).addTo(map);

                var layersData = {json.dumps(tif_layers_json)};
                var globalBounds = null;

                layersData.forEach(function(layerInfo, index) {{
                    var layer = L.imageOverlay(
                        layerInfo.url, layerInfo.bounds, {{ opacity: 0.85 }}
                    );

                    if (index === 0) {{
                        layer.addTo(map);
                    }}

                    layerControl.addOverlay(layer, layerInfo.name);

                    var lBounds = L.latLngBounds(layerInfo.bounds);
                    if (!globalBounds) {{
                        globalBounds = lBounds;
                    }} else {{
                        globalBounds.extend(lBounds);
                    }}
                }});

                if (globalBounds) {{
                    map.fitBounds(globalBounds);
                }}
            </script>
            """

    map_count = sum([show_sat, show_opera, show_drcs])
    if uses_disasters and tif_layers_json:
        map_count += 1

    iframe_width = f"{100 // map_count}%" if map_count else "100%"

    page_html = f"""
    <html>
      <head>
        <title>Results</title>
        <style>
          body {{ display:flex; flex-direction: column; margin:0;
                  height:100vh; font-family:sans-serif;
                  background:#f3f4f6; }}
          pre {{ flex:0 0 25%; overflow:auto; padding:15px; margin:0;
                 background:#111; color:#10b981; font-size:12px;
                 border-bottom:2px solid #374151; }}
          .maps-row {{ display:flex; flex:1; background:white; }}
          iframe {{ width:{iframe_width}; height:100%; border:none;
                    border-right:1px solid #d1d5db; }}
        </style>
      </head>
      <body>
        {log_html}
        <div class="maps-row">
            {iframes}
            {geotiff_viewer}
        </div>
      </body>
    </html>
    """
    return render_template_string(page_html)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)

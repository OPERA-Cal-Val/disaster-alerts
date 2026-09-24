"""
Plot interactive HTML map for the activated events.

- add detailed description later .
"""

from __future__ import annotations

import colorsys
import hashlib
import html
import ipaddress
import json
import logging
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import requests

from .settings import Settings

# -----------------------------------------------------------------------------
# generate and save an interactive HTML map
# -----------------------------------------------------------------------------


Event = Dict[str, Any]
log = logging.getLogger(__name__)

FAMILY_HUES = {
    "flood": 210 / 360,  # blue
    "hurricane": 120 / 360,  # green
    "storm": 10 / 360,  # red/orange
    "thunderstorm": 270 / 360,  # purple
    "earthquake": 45 / 360,  # amber
}

TRUSTED_URL_SUFFIXES = (
    "weather.gov",
    "noaa.gov",
    "usgs.gov",
)
MAX_GEOJSON_BYTES = 2 * 1024 * 1024  # 2 MiB


def _is_url(s: str) -> bool:
    parsed = urlparse(s)
    return parsed.scheme in ("http", "https")


def _host_is_trusted(hostname: str) -> bool:
    host = hostname.lower().strip(".")
    return any(
        host == suffix or host.endswith(f".{suffix}") for suffix in TRUSTED_URL_SUFFIXES
    )


def _host_resolves_public(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False

    for _, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


def _validate_remote_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Only https URLs are allowed for AOI downloads")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if not _host_is_trusted(parsed.hostname):
        raise ValueError(f"Untrusted host for AOI download: {parsed.hostname}")
    if not _host_resolves_public(parsed.hostname):
        raise ValueError(f"Host resolves to a non-public address: {parsed.hostname}")


def _detect_family(event_type: str) -> str:
    s = event_type.lower()
    if "flood" in s:
        return "flood"
    if "hurricane" in s:
        return "hurricane"
    if "earthquake" in s:
        return "earthquake"
    if "thunderstorm" in s:
        return "thunderstorm"
    # keep this order otherwise thunderstorm events
    # will be categorized as storm
    if "storm" in s:
        return "storm"
    return "storm"


def _color_from_event_type(event_type: str) -> str:
    family = _detect_family(event_type)
    base_hue = FAMILY_HUES[family]

    hash_hex = hashlib.md5(event_type.encode()).hexdigest()

    # Use more hash bits for stronger variation
    h_variation = int(hash_hex[:2], 16) / 255.0  # 0–1
    l_variation = int(hash_hex[2:4], 16) / 255.0  # 0–1

    # --- HUE variation (±15 degrees) ---
    hue_offset = (h_variation - 0.5) * (30 / 360)  # ±15°
    hue = (base_hue + hue_offset) % 1.0

    # --- LIGHTNESS variation (wide range) ---
    lightness = 0.35 + 0.35 * l_variation  # 0.35–0.70

    saturation = 0.75

    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)

    return "#{:02x}{:02x}{:02x}".format(
        int(r * 255),
        int(g * 255),
        int(b * 255),
    )


def _magnitude_to_radius(mag: float | None) -> float:
    """Map earthquake magnitude to a CircleMarker pixel radius.

    Uses a super-linear curve so higher magnitudes stand out visually,
    clamped so M<3 quakes stay visible and M>7.5 quakes don't dominate.
    """
    if mag is None:
        return 5.0
    base = max(0.0, float(mag) - 1.0)
    return max(4.0, min(22.0, 1.6 * base**1.4))


def _generate_events_html_map(
    settings: "Settings",
    events: dict[str, list["Event"]],
    file_dir: "Path",
):
    """
    Create an interactive map displaying activated events,
    grouped by routing key, and enabling the user to draw a bounding box.
    """

    import folium
    from branca.element import MacroElement
    from folium.features import GeoJson
    from folium.plugins import Draw
    from jinja2 import Template
    from shapely.geometry import MultiPolygon, Point

    class MapDashboardJS(MacroElement):
        def __init__(self):
            super().__init__()
            self._template = Template(
                r"""
                {% macro html(this, kwargs) %}
                <style>
                    #control-panel {
                        position: absolute; top: 20px; left: 60px; z-index: 1000;
                        background: rgba(255, 255, 255, 0.95); padding: 12px 20px;
                        border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.2);
                        display: flex; flex-direction: column; gap: 12px;
                        border: 2px solid #374151; font-family: Arial, sans-serif;
                        max-width: 900px;
                    }
                    .main-row {
                        display: flex; flex-direction: row; gap: 12px;
                        align-items: center; width: 100%; justify-content: flex-start;
                        flex-wrap: nowrap;
                    }
                    .panel-label {
                        font-size: 14px; font-weight: bold; color: #374151;
                        white-space: nowrap;
                    }
                    .settings-panel {
                        display: flex; gap: 12px; align-items: center; padding: 10px;
                        background: #f3f4f6; border-radius: 6px; flex-wrap: wrap;
                        border: 1px dashed #9ca3af; width: 100%; box-sizing: border-box;
                    }
                    #control-panel select, #control-panel input {
                        border: 1px solid #9ca3af; border-radius: 4px; padding: 4px 8px;
                        font-size: 13px; background-color: #ffffff; height: 34px;
                        box-sizing: border-box;
                    }
                    #control-panel input[readonly] {
                        background-color: #f3f4f6; color: #6b7280;
                        cursor: not-allowed;
                    }
                    /* Disable the Leaflet Draw toolbar when the AOI mode
                       isn't 'draw' so users can't accidentally add a
                       rectangle that no other mode would react to. */
                    .leaflet-draw.aoi-draw-disabled {
                        opacity: 0.4;
                        pointer-events: none;
                    }
                    /* #control-panel overlays the map at z-index 1000 and the
                       draw toolbar's Save/Cancel flyout opens rightwards
                       underneath it, which swallows those clicks. Leaflet's
                       .leaflet-top is itself z-index 1000 and creates the
                       stacking context, so it's the one that has to be
                       lifted — a z-index on .leaflet-draw can't escape it.
                       Only the narrow left control column moves, so nothing
                       in the panel gets covered. */
                    .leaflet-top.leaflet-left {
                        z-index: 1001;
                    }
                    /* Point-placement tool: show an "x" instead of the
                       default marker-pin icon so it reads as "place a
                       point" rather than "drop a pin/marker". */
                    .leaflet-draw-toolbar a.leaflet-draw-draw-marker {
                        background-image: none !important;
                        display: flex; align-items: center;
                        justify-content: center;
                    }
                    .leaflet-draw-toolbar a.leaflet-draw-draw-marker::before {
                        content: "\2715";
                        font-size: 16px; font-weight: bold; color: #374151;
                    }
                    .aoi-point-x {
                        font-size: 20px; font-weight: bold; color: #b91c1c;
                        line-height: 22px; text-align: center;
                        text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff;
                    }
                    /* Progress overlay shown after SEARCH is submitted. */
                    #progress-overlay {
                        position: fixed; inset: 0; z-index: 5000;
                        background: rgba(15, 23, 42, 0.55);
                        display: none; align-items: center;
                        justify-content: center;
                        font-family: Arial, sans-serif;
                    }
                    #progress-overlay.visible { display: flex; }
                    .progress-card {
                        background: #ffffff; padding: 22px 28px;
                        border-radius: 10px; min-width: 360px; max-width: 480px;
                        box-shadow: 0 12px 32px rgba(0,0,0,0.25);
                    }
                    .progress-title {
                        display: flex; align-items: center; gap: 10px;
                        font-size: 16px; font-weight: bold; color: #111827;
                    }
                    .progress-title #progress-elapsed {
                        margin-left: auto; color: #6b7280;
                        font-variant-numeric: tabular-nums;
                    }
                    #progress-stage {
                        margin: 10px 0 4px; color: #374151; font-size: 14px;
                    }
                    #progress-count {
                        color: #059669;
                        font-variant-numeric: tabular-nums; font-size: 13px;
                        min-height: 16px;
                    }
                    .progress-bar-outer {
                        margin-top: 8px; background: #e5e7eb; border-radius: 6px;
                        height: 12px; overflow: hidden;
                    }
                    .progress-bar-inner {
                        height: 100%; width: 0%; background: #059669;
                        transition: width 0.4s ease;
                    }
                    .progress-bar-inner.indeterminate {
                        width: 40%;
                        animation: progress-indet 1.4s ease-in-out infinite;
                    }
                    @keyframes progress-indet {
                        0%   { margin-left: -40%; }
                        100% { margin-left: 100%; }
                    }
                    .progress-spinner {
                        display: inline-block; width: 14px; height: 14px;
                        border-radius: 50%; border: 2px solid #d1d5db;
                        border-top-color: #059669;
                        animation: progress-spin 0.9s linear infinite;
                    }
                    @keyframes progress-spin {
                        to { transform: rotate(360deg); }
                    }
                    .progress-hint {
                        margin-top: 10px; font-size: 12px; color: #6b7280;
                    }
                    .multi-dropdown {
                        position: relative; display: inline-block;
                    }
                    .multi-dropdown-btn {
                        position: relative;
                        border: 1px solid #9ca3af; border-radius: 4px;
                        padding: 4px 24px 4px 8px; font-size: 13px;
                        background-color: #ffffff; height: 34px; cursor: pointer;
                        width: 160px; text-align: left; overflow: hidden;
                        text-overflow: ellipsis; white-space: nowrap;
                    }
                    .multi-dropdown-btn::after {
                        content: " ▾"; position: absolute; right: 8px; top: 8px;
                    }
                    .multi-dropdown-list {
                        display: none; position: absolute; top: 36px; left: 0;
                        background: #ffffff; border: 1px solid #9ca3af;
                        border-radius: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);
                        z-index: 9999; min-width: 100%; padding: 4px 0;
                        max-height: 250px; overflow-y: auto;
                    }
                    .multi-dropdown-list.open {
                        display: block;
                    }
                    .multi-dropdown-list label {
                        display: flex; align-items: center; gap: 8px; padding: 6px 12px;
                        font-size: 13px; cursor: pointer; white-space: nowrap;
                    }
                    .multi-dropdown-list label:hover {
                        background-color: #f3f4f6;
                    }
                    #action-btn {
                        background-color: #4b5563; color: white; border: none;
                        padding: 0 25px; border-radius: 4px; font-weight: bold;
                        font-size: 14px; cursor: pointer; height: 34px;
                        white-space: nowrap; transition: background-color 0.2s;
                    }
                    #action-btn:hover {
                        background-color: #374151;
                    }
                    .dynamic-opt {
                        display: flex; align-items: center; gap: 6px; font-size: 13px;
                        background: #e5e7eb; padding: 4px 8px; border-radius: 4px;
                        border: 1px solid #d1d5db;
                    }
                    .radio-group {
                        display: flex; gap: 15px; align-items: center; font-size: 13px;
                        background: #ffffff; padding: 4px 10px; border-radius: 4px;
                        border: 1px solid #9ca3af; height: 34px; box-sizing: border-box;
                    }
                    .radio-group label {
                        display: flex; align-items: center; gap: 4px; cursor: pointer;
                    }
                </style>

                <div id="progress-overlay">
                    <div class="progress-card">
                        <div class="progress-title">
                            <span class="progress-spinner"></span>
                            <span>Processing</span>
                            <span id="progress-elapsed">00:00</span>
                        </div>
                        <div id="progress-stage">Starting…</div>
                        <div id="progress-count"></div>
                        <div class="progress-bar-outer">
                            <div id="progress-bar-inner"
                                class="progress-bar-inner indeterminate"></div>
                        </div>
                        <div class="progress-hint">
                            You'll be redirected when done.
                        </div>
                    </div>
                </div>

                <div id="control-panel">
                    <div class="main-row">
                        <span class="panel-label">Workflow:</span>

                        <div class="multi-dropdown" id="func-dropdown">
                            <button type="button" class="multi-dropdown-btn"
                                id="func-btn">All Functionality</button>
                            <div class="multi-dropdown-list" id="func-list">
                                <label><input type="checkbox" value="all" checked>
                                    All</label>
                                <label><input type="checkbox" value="overpasses">
                                    Overpasses</label>
                                <label><input type="checkbox" value="opera_search">
                                    Opera Search</label>
                                <label><input type="checkbox" value="disasters">
                                    Disasters Workflow</label>
                            </div>
                        </div>

                        <div class="multi-dropdown" id="prod-dropdown">
                            <button type="button" class="multi-dropdown-btn"
                                id="prod-btn">All Products</button>
                            <div class="multi-dropdown-list" id="prod-list">
                                <label><input type="checkbox" value="all" checked>
                                    All Products</label>
                                <label><input type="checkbox"
                                    value="OPERA_L3_DSWX-HLS_V1"> DSWX-HLS</label>
                                <label><input type="checkbox"
                                    value="OPERA_L3_DSWX-S1_V1"> DSWX-S1</label>
                                <label><input type="checkbox"
                                    value="OPERA_L3_DIST-ALERT-HLS_V1">
                                    DIST-ALERT-HLS</label>
                                <label><input type="checkbox"
                                    value="OPERA_L3_DIST-ANN-HLS_V1">
                                    DIST-ANN-HLS</label>
                                <label><input type="checkbox"
                                    value="OPERA_L2_RTC-S1_V1"> RTC-S1</label>
                                <label><input type="checkbox"
                                    value="OPERA_L2_CSLC-S1_V1"> CSLC-S1</label>
                                <label><input type="checkbox"
                                    value="OPERA_L3_DISP-S1_V1"> DISP-S1</label>
                            </div>
                        </div>

                        <div style="flex-grow: 1;"></div>
                        <button id="action-btn">SEARCH</button>
                    </div>

                    <div id="aoi-panel" class="settings-panel">
                        <span class="panel-label">↳ AOI:</span>
                        <div class="radio-group" id="aoi-mode-group">
                            <label><input type="radio" name="aoi_mode" value="draw"
                                checked> Draw</label>
                            <label><input type="radio" name="aoi_mode" value="coords">
                                Coords</label>
                            <label><input type="radio" name="aoi_mode" value="wkt">
                                WKT</label>
                            <label><input type="radio" name="aoi_mode" value="url">
                                URL</label>
                            <label><input type="radio" name="aoi_mode" value="file">
                                File</label>
                        </div>
                        <div id="aoi-coords"
                            style="display:none; gap:6px; align-items:center;">
                            <input id="aoi_lat_min" type="number" step="any"
                                placeholder="lat_min" style="width:90px;">
                            <input id="aoi_lat_max" type="number" step="any"
                                placeholder="lat_max" style="width:90px;">
                            <input id="aoi_lon_min" type="number" step="any"
                                placeholder="lon_min" style="width:90px;">
                            <input id="aoi_lon_max" type="number" step="any"
                                placeholder="lon_max" style="width:90px;">
                        </div>
                        <input id="aoi_wkt" type="text"
                            placeholder="POINT(lon lat) or POLYGON((lon lat, ...))"
                            style="display:none; width:340px;">
                        <input id="aoi_url" type="url"
                            placeholder="https://... .geojson"
                            style="display:none; width:340px;">
                        <input id="aoi_file" type="file"
                            accept=".geojson,.json,.kml" style="display:none;">
                        <span id="aoi_status"
                            style="font-size:12px; color:#374151;"></span>
                    </div>

                    <div id="next-pass-panel" class="settings-panel"
                        <span class="panel-label">↳ Next Pass:</span>

                        <div class="multi-dropdown" id="sat-dropdown">
                            <button type="button" class="multi-dropdown-btn"
                                id="sat-btn" style="width: 140px;">All Satellites
                            </button>
                            <div class="multi-dropdown-list" id="sat-list">
                                <label><input type="checkbox" value="all" checked>
                                    All Satellites</label>
                                <label><input type="checkbox" value="sentinel-1">
                                    Sentinel-1</label>
                                <label><input type="checkbox" value="sentinel-2">
                                    Sentinel-2</label>
                                <label><input type="checkbox" value="landsat">
                                    Landsat</label>
                                <label><input type="checkbox" value="nisar">
                                    NISAR</label>
                            </div>
                        </div>

                        <input type="number" id="np_lookback"
                            placeholder="Lookback (days)" min="1" max="30"
                            style="width: 130px;">

                        <select id="drcs_enabled" onchange="toggleDrcsDate(this.value)"
                            style="width: 100px;">
                            <option value="no">DRCS: No</option>
                            <option value="yes">DRCS: Yes</option>
                        </select>
                        <input type="text" id="drcs_event_date"
                            placeholder="Event: YYYY-MM-DDTHH:MM" disabled
                            style="width: 170px;">
                    </div>

                    <div id="disasters-panel" class="settings-panel"
                        style="display: none;">
                        <span class="panel-label">↳ Disasters:</span>

                        <select id="dis_action"
                            style="font-weight: bold; background-color: #e5e7eb;">
                            <option value="run" selected>
                                Full Pipeline (Maps & Layouts)
                            </option>
                            <option value="mosaic">Mosaic GeoTIFFs Only</option>
                            <option value="download">Download Granules Only</option>
                        </select>

                        <div class="radio-group">
                            <label><input type="radio" name="date_strat" value="range"
                                checked onchange="toggleDisDateStrat()"> Range</label>
                            <label><input type="radio" name="date_strat" value="single"
                                onchange="toggleDisDateStrat()"> Single</label>
                            <label><input type="radio" name="date_strat" value="recent"
                                onchange="toggleDisDateStrat()"> Recent</label>
                        </div>

                        <div id="dis_range_div"
                            style="display:flex; align-items:center; gap:6px;">
                            <input type="date" id="dis_start_date"> <span>to</span>
                            <input type="date" id="dis_end_date">
                        </div>
                        <input type="date" id="dis_single_date" style="display:none;">
                        <input type="number" id="dis_recent_n" placeholder="# passes"
                            min="1" style="display:none; width: 100px;">

                        <div id="dis_adv_options"
                            style="display:flex; gap:10px; margin-left: auto;">
                            <label class="dynamic-opt">
                                <input type="checkbox" id="opt_nomask"> No Mask
                            </label>
                            <label class="dynamic-opt" id="wrap_rc"
                                style="display:none;">
                                <input type="checkbox" id="opt_rc"> Reclassify Snow/Ice
                            </label>
                            <label class="dynamic-opt" id="wrap_cloud"
                                style="display:none;">
                                <input type="checkbox" id="opt_cloud"> Calc Cloudiness
                            </label>

                            <div class="dynamic-opt" id="wrap_fd"
                                style="display:none; height:34px;
                                box-sizing:border-box;">
                                Filter (-fd): <input type="date" id="opt_fd"
                                    style="height:24px; border:none; padding:0 4px;">
                            </div>
                            <div class="dynamic-opt" id="wrap_st"
                                style="display:none; height:34px;
                                box-sizing:border-box;">
                                Slope (-st): <input type="number" id="opt_st"
                                    placeholder="Deg" min="0" max="100"
                                    style="width:50px; height:24px; border:none;
                                    padding:0 4px;">
                            </div>
                        </div>
                    </div>
                </div>
                {% endmacro %}

                {% macro script(this, kwargs) %}
                var currentBbox = null;
                var currentBboxLayer = null;
                var justDrawn = false;

                function setupMultiDropdown(btnId, listId, allValue, defaultLabel) {
                    var btn = document.getElementById(btnId);
                    var list = document.getElementById(listId);
                    var checkboxes = list.querySelectorAll('input[type=checkbox]');
                    var allBox = list.querySelector('input[value="' + allValue + '"]');

                    btn.addEventListener('click', function(e) {
                        e.stopPropagation();
                        var active = '.multi-dropdown-list.open';
                        var opens = document.querySelectorAll(active);
                        opens.forEach(function(el) {
                            if (el !== list) el.classList.remove('open');
                        });
                        list.classList.toggle('open');
                    });

                    checkboxes.forEach(function(cb) {
                        cb.addEventListener('change', function() {
                            if (cb === allBox && cb.checked) {
                                checkboxes.forEach(function(c) { c.checked = false; });
                                allBox.checked = true;
                            } else if (cb !== allBox && cb.checked) {
                                allBox.checked = false;
                            }
                            updateLabel();
                            if (listId === 'func-list') updatePanels();
                            if (listId === 'prod-list') updateDisasterOptions();
                        });
                    });

                    function updateLabel() {
                        if (allBox.checked) {
                            btn.textContent = defaultLabel;
                            return;
                        }
                        var sel = Array.from(checkboxes)
                            .filter(c => c.checked && c !== allBox)
                            .map(c => c.value);
                        btn.textContent = sel.length ? sel.join(', ') : defaultLabel;
                    }
                }

                function getMultiValues(listId, allValue) {
                    var list = document.getElementById(listId);
                    var allBox = list.querySelector('input[value="' + allValue + '"]');
                    if (allBox.checked) return [allValue];
                    var cbxs = list.querySelectorAll('input[type=checkbox]');
                    return Array.from(cbxs).filter(c => c.checked).map(c => c.value);
                }

                document.addEventListener('click', function() {
                    var active = '.multi-dropdown-list.open';
                    var opens = document.querySelectorAll(active);
                    opens.forEach(el => el.classList.remove('open'));
                });

                setupMultiDropdown(
                    'func-btn', 'func-list', 'all', 'All Functionality'
                );
                setupMultiDropdown(
                    'sat-btn', 'sat-list', 'all', 'All Satellites'
                );
                setupMultiDropdown(
                    'prod-btn', 'prod-list', 'all', 'All Products'
                );

                // --- UI Toggles ---
                function updatePanels() {
                    var funcs = getMultiValues('func-list', 'all');
                    var showNextPass = funcs.includes('all') ||
                                       funcs.includes('overpasses') ||
                                       funcs.includes('opera_search');
                    var showDisasters = funcs.includes('all') ||
                                        funcs.includes('disasters');

                    // Determine if the Products dropdown should be visible
                    var showProducts = funcs.includes('all') ||
                                       funcs.includes('opera_search') ||
                                       funcs.includes('disasters');

                    var nextPanel = document.getElementById('next-pass-panel');
                    nextPanel.style.display = showNextPass ? 'flex' : 'none';

                    var disPanel = document.getElementById('disasters-panel');
                    disPanel.style.display = showDisasters ? 'flex' : 'none';

                    // Toggle the Products dropdown
                    var prodDrop = document.getElementById('prod-dropdown');
                    prodDrop.style.display = showProducts ? 'inline-block' : 'none';

                    // Update Button Text
                    var btn = document.getElementById('action-btn');
                    if (showDisasters && !showNextPass) {
                        btn.textContent = "RUN DISASTERS";
                        btn.style.backgroundColor = "#059669"; // Green
                    } else {
                        btn.textContent = "SEARCH";
                        btn.style.backgroundColor = "#4b5563"; // Default Gray
                    }
                }

                function toggleDrcsDate(val) {
                    var input = document.getElementById('drcs_event_date');
                    input.disabled = (val !== 'yes');
                }

                function toggleDisDateStrat() {
                    var sel = 'input[name="date_strat"]:checked';
                    var val = document.querySelector(sel).value;
                    var dRec = document.getElementById('dis_recent_n');
                    var dSin = document.getElementById('dis_single_date');
                    var dRan = document.getElementById('dis_range_div');

                    dRec.style.display = (val === 'recent') ? 'block' : 'none';
                    dSin.style.display = (val === 'single') ? 'block' : 'none';
                    dRan.style.display = (val === 'range') ? 'flex' : 'none';
                }

                // --- Product-Aware Logic ---
                function updateDisasterOptions() {
                    var prods = getMultiValues('prod-list', 'all');
                    var isAll = prods.includes('all');

                    var hasHLS = isAll || prods.some(p => p.includes('HLS'));
                    var hasDSWxHLS = isAll || prods.some(p => p.includes('DSWX-HLS'));
                    var hasDIST = isAll || prods.some(p => p.includes('DIST'));
                    var hasRTC = isAll || prods.some(
                        p => p.includes('RTC') || p.includes('S1')
                    );

                    var wCloud = document.getElementById('wrap_cloud');
                    var wRc = document.getElementById('wrap_rc');
                    var wFd = document.getElementById('wrap_fd');
                    var wSt = document.getElementById('wrap_st');

                    wCloud.style.display = hasHLS ? 'flex' : 'none';
                    wRc.style.display = hasDSWxHLS ? 'flex' : 'none';
                    wFd.style.display = hasDIST ? 'flex' : 'none';
                    wSt.style.display = hasRTC ? 'flex' : 'none';
                }

                // Initialize UI
                updatePanels();
                toggleDisDateStrat();
                updateDisasterOptions();

                // --- Map Box / Point Drawing ---
                // Replace the marker tool's default pin icon with an "x", and
                // relabel it, so it reads as "place a point" rather than
                // "drop a map pin".
                if (typeof L !== 'undefined' && L.Draw && L.Draw.Marker) {
                    L.Draw.Marker.prototype.options.icon = L.divIcon({
                        className: 'aoi-point-icon',
                        html: '<div class="aoi-point-x">&times;</div>',
                        iconSize: [22, 22],
                        iconAnchor: [11, 11]
                    });
                }
                if (typeof L !== 'undefined' && L.drawLocal) {
                    L.drawLocal.draw.handlers.marker.tooltip.start =
                        'Click map to place a point.';
                    L.drawLocal.draw.toolbar.buttons.marker = 'Place a point';
                }
                // Both strings above are consumed while the Draw control is
                // constructed, which already happened by the time this runs,
                // so each needs a nudge: the button title is patched on the
                // rendered DOM, and the cursor tooltip — cached per handler
                // instance in initialize() — is refreshed on enable.
                var pointToolBtn = document.querySelector(
                    'a.leaflet-draw-draw-marker'
                );
                if (pointToolBtn) pointToolBtn.title = 'Place a point';
                if (typeof L !== 'undefined' && L.Draw && L.Draw.Marker) {
                    var markerAddHooks = L.Draw.Marker.prototype.addHooks;
                    L.Draw.Marker.prototype.addHooks = function() {
                        this._initialLabelText =
                            L.drawLocal.draw.handlers.marker.tooltip.start;
                        return markerAddHooks.call(this);
                    };
                }

                function bboxFromLayer(layer) {
                    if (layer.getLatLng) {
                        // Point AOI: lat/lon collapsed to a single coordinate,
                        // exactly like a WKT POINT(...) input.
                        var ll = layer.getLatLng();
                        return {
                            lat_min: ll.lat,
                            lat_max: ll.lat,
                            lon_min: ll.lng,
                            lon_max: ll.lng
                        };
                    }
                    var bounds = layer.getBounds();
                    return {
                        lat_min: bounds.getSouth(),
                        lat_max: bounds.getNorth(),
                        lon_min: bounds.getWest(),
                        lon_max: bounds.getEast()
                    };
                }

                {{this._parent.get_name()}}.on('draw:created', function(e) {
                    if (currentBboxLayer) {
                        {{this._parent.get_name()}}.removeLayer(currentBboxLayer);
                    }
                    currentBboxLayer = e.layer;
                    currentBboxLayer.addTo({{this._parent.get_name()}});
                    currentBbox = bboxFromLayer(currentBboxLayer);
                    justDrawn = true;
                    if (currentAoiMode() === 'draw') syncDrawCoords();
                });

                // Saving an edit has to re-read the shape, otherwise SEARCH
                // submits the coordinates from before the drag. Only the save
                // event is handled on purpose: Cancel reverts the layer
                // without notifying, so a live update would strand the
                // dragged-to position in currentBbox.
                {{this._parent.get_name()}}.on('draw:edited', function() {
                    if (!currentBboxLayer) return;
                    currentBbox = bboxFromLayer(currentBboxLayer);
                    if (currentAoiMode() === 'draw') syncDrawCoords();
                });

                // Likewise a deleted shape must not leave its coordinates
                // behind as an invisible AOI.
                {{this._parent.get_name()}}.on('draw:deleted', function(e) {
                    var removed = false;
                    e.layers.eachLayer(function(l) {
                        if (l === currentBboxLayer) removed = true;
                    });
                    if (!removed) return;
                    currentBboxLayer = null;
                    currentBbox = null;
                    if (currentAoiMode() === 'draw') syncDrawCoords();
                });

                {{this._parent.get_name()}}.on('click', function() {
                    if (justDrawn) {
                        justDrawn = false;
                        return;
                    }
                    if (currentBboxLayer) {
                        {{this._parent.get_name()}}.removeLayer(currentBboxLayer);
                        currentBboxLayer = null;
                        currentBbox = null;
                        if (currentAoiMode() === 'draw') syncDrawCoords();
                    }
                });

                // --- AOI Input Handling (Coords / WKT / URL / File) ---
                var previewLayer = null;
                var uploadedFile = null;

                function setAoiStatus(msg, isError) {
                    var el = document.getElementById('aoi_status');
                    el.textContent = msg || '';
                    el.style.color = isError ? '#dc2626' : '#374151';
                }
                function clearPreview() {
                    if (previewLayer) {
                        {{this._parent.get_name()}}.removeLayer(previewLayer);
                        previewLayer = null;
                    }
                }
                function clearDrawnBox() {
                    if (currentBboxLayer) {
                        {{this._parent.get_name()}}.removeLayer(currentBboxLayer);
                        currentBboxLayer = null;
                        currentBbox = null;
                    }
                }
                function currentAoiMode() {
                    return document.querySelector(
                        'input[name="aoi_mode"]:checked'
                    ).value;
                }
                function showAoiInput(mode) {
                    var ids = ['aoi-coords', 'aoi_wkt', 'aoi_url', 'aoi_file'];
                    ids.forEach(function(id) {
                        document.getElementById(id).style.display = 'none';
                    });
                    if (mode === 'coords' || mode === 'draw') {
                        document.getElementById('aoi-coords').style.display = 'flex';
                    } else if (mode === 'wkt') {
                        document.getElementById('aoi_wkt').style.display =
                            'inline-block';
                    } else if (mode === 'url') {
                        document.getElementById('aoi_url').style.display =
                            'inline-block';
                    } else if (mode === 'file') {
                        document.getElementById('aoi_file').style.display =
                            'inline-block';
                    }
                    // Draw mode shows the same fields as Coords but read-only,
                    // populated by the Leaflet Draw handler.
                    var readOnly = (mode === 'draw');
                    ['aoi_lat_min', 'aoi_lat_max', 'aoi_lon_min', 'aoi_lon_max']
                        .forEach(function(id) {
                            document.getElementById(id).readOnly = readOnly;
                        });
                    if (mode === 'draw') {
                        // Draw owns currentBboxLayer; any lingering
                        // typed-input preview goes so one shape shows.
                        clearPreview();
                        syncDrawCoords();
                    } else if (mode === 'coords') {
                        // Green preview from a prior wkt/url/file visit
                        // shouldn't hang around under coord editing.
                        // Drawn blue box is intentionally kept for the
                        // Draw ↔ Coords adjust flow.
                        clearPreview();
                    } else {
                        // wkt / url / file: fresh canvas.
                        clearPreview();
                        clearDrawnBox();
                        syncDrawCoords();
                    }
                    // Match the drawn box's colour to the mode it now
                    // belongs to: Leaflet-Draw blue in Draw, green in
                    // Coords (matches previewLayer styling).
                    if (currentBboxLayer && currentBboxLayer.setStyle) {
                        if (mode === 'coords') {
                            currentBboxLayer.setStyle({
                                color: '#059669',
                                weight: 2,
                                fillOpacity: 0.1
                            });
                        } else if (mode === 'draw') {
                            currentBboxLayer.setStyle({
                                color: '#3388ff',
                                weight: 4,
                                fillOpacity: 0.2
                            });
                        }
                    }
                    // Disable the Leaflet Draw toolbar outside Draw mode.
                    var drawCtrl = document.querySelector('.leaflet-draw');
                    if (drawCtrl) {
                        if (mode === 'draw') {
                            drawCtrl.classList.remove('aoi-draw-disabled');
                        } else {
                            drawCtrl.classList.add('aoi-draw-disabled');
                        }
                    }
                    // Reset every other mode's DOM input value so switching
                    // back to them later doesn't resurrect stale text/files.
                    if (mode !== 'wkt') {
                        document.getElementById('aoi_wkt').value = '';
                    }
                    if (mode !== 'url') {
                        document.getElementById('aoi_url').value = '';
                    }
                    if (mode !== 'file') {
                        document.getElementById('aoi_file').value = '';
                        uploadedFile = null;
                    }
                    setAoiStatus('', false);
                }

                function syncDrawCoords() {
                    var la1 = document.getElementById('aoi_lat_min');
                    var la2 = document.getElementById('aoi_lat_max');
                    var lo1 = document.getElementById('aoi_lon_min');
                    var lo2 = document.getElementById('aoi_lon_max');
                    if (currentBbox) {
                        la1.value = currentBbox.lat_min.toFixed(4);
                        la2.value = currentBbox.lat_max.toFixed(4);
                        lo1.value = currentBbox.lon_min.toFixed(4);
                        lo2.value = currentBbox.lon_max.toFixed(4);
                    } else {
                        la1.value = ''; la2.value = '';
                        lo1.value = ''; lo2.value = '';
                    }
                }
                document.querySelectorAll('input[name="aoi_mode"]').forEach(
                    function(radio) {
                        radio.addEventListener('change', function() {
                            showAoiInput(currentAoiMode());
                        });
                    }
                );
                showAoiInput(currentAoiMode());

                function previewCoords() {
                    var la1 = parseFloat(
                        document.getElementById('aoi_lat_min').value
                    );
                    var la2 = parseFloat(
                        document.getElementById('aoi_lat_max').value
                    );
                    var lo1 = parseFloat(
                        document.getElementById('aoi_lon_min').value
                    );
                    var lo2 = parseFloat(
                        document.getElementById('aoi_lon_max').value
                    );
                    if ([la1, la2, lo1, lo2].some(isNaN)) return;
                    clearPreview();
                    clearDrawnBox();
                    previewLayer = L.rectangle(
                        [[la1, lo1], [la2, lo2]],
                        {color: '#059669', weight: 2, fillOpacity: 0.1}
                    );
                    previewLayer.addTo({{this._parent.get_name()}});
                    {{this._parent.get_name()}}.fitBounds(previewLayer.getBounds());
                }
                ['aoi_lat_min', 'aoi_lat_max', 'aoi_lon_min', 'aoi_lon_max']
                    .forEach(function(id) {
                        document.getElementById(id).addEventListener(
                            'change', previewCoords
                        );
                    });

                function parseWkt(s) {
                    s = s.trim();
                    var m = /^POINT\s*\(\s*(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s*\)$/i
                        .exec(s);
                    if (m) {
                        return {
                            type: 'Point',
                            lon: parseFloat(m[1]),
                            lat: parseFloat(m[2])
                        };
                    }
                    m = /^POLYGON\s*\(\s*\(\s*(.+?)\s*\)\s*\)$/i.exec(s);
                    if (m) {
                        var pts = m[1].split(',').map(function(pair) {
                            var parts = pair.trim().split(/\s+/);
                            return [parseFloat(parts[1]), parseFloat(parts[0])];
                        });
                        var bad = pts.some(function(p) {
                            return isNaN(p[0]) || isNaN(p[1]);
                        });
                        if (bad) return null;
                        return {type: 'Polygon', pts: pts};
                    }
                    return null;
                }
                document.getElementById('aoi_wkt').addEventListener(
                    'change', function() {
                        var v = this.value.trim();
                        if (!v) { clearPreview(); setAoiStatus('', false); return; }
                        var parsed = parseWkt(v);
                        if (!parsed) {
                            setAoiStatus('Invalid WKT', true);
                            return;
                        }
                        clearPreview();
                        clearDrawnBox();
                        if (parsed.type === 'Point') {
                            previewLayer = L.marker([parsed.lat, parsed.lon]);
                            previewLayer.addTo({{this._parent.get_name()}});
                            {{this._parent.get_name()}}.setView(
                                [parsed.lat, parsed.lon], 8
                            );
                        } else {
                            previewLayer = L.polygon(parsed.pts, {
                                color: '#059669', weight: 2, fillOpacity: 0.1
                            });
                            previewLayer.addTo({{this._parent.get_name()}});
                            {{this._parent.get_name()}}.fitBounds(
                                previewLayer.getBounds()
                            );
                        }
                        setAoiStatus('WKT parsed OK', false);
                    }
                );

                function renderPreviewGeoJson(geom, successMsg) {
                    clearPreview();
                    clearDrawnBox();
                    previewLayer = L.geoJSON(geom, {style: {
                        color: '#059669', weight: 2, fillOpacity: 0.1
                    }});
                    previewLayer.addTo({{this._parent.get_name()}});
                    var b = previewLayer.getBounds();
                    if (b.isValid()) {
                        {{this._parent.get_name()}}.fitBounds(b);
                    }
                    setAoiStatus(successMsg, false);
                }

                function fetchAoiPreview(fetchInit, successMsg, errPrefix) {
                    fetch('/aoi_preview', fetchInit)
                        .then(function(r) {
                            return r.json().then(function(body) {
                                return {ok: r.ok, body: body};
                            });
                        })
                        .then(function(resp) {
                            if (!resp.ok) {
                                setAoiStatus(
                                    errPrefix + ': ' +
                                    (resp.body.error || 'preview failed'),
                                    true
                                );
                                return;
                            }
                            renderPreviewGeoJson(resp.body.geometry, successMsg);
                        })
                        .catch(function(e) {
                            if (typeof console !== 'undefined' && console.error) {
                                console.error(errPrefix, e);
                            }
                            setAoiStatus(
                                errPrefix + ': ' + (e.name || 'Error') +
                                ' — ' + (e.message || 'no details'),
                                true
                            );
                        });
                }

                document.getElementById('aoi_url').addEventListener(
                    'change', function() {
                        var v = this.value.trim();
                        if (!v) {
                            clearPreview();
                            setAoiStatus('', false);
                            return;
                        }
                        if (!/^https:\/\//i.test(v)) {
                            setAoiStatus('URL must start with https://', true);
                            return;
                        }
                        setAoiStatus('Fetching preview…', false);
                        fetchAoiPreview(
                            {
                                method: 'POST',
                                headers: {'Content-Type': 'application/json'},
                                body: JSON.stringify({
                                    aoi: {kind: 'url', value: v}
                                })
                            },
                            'URL preview loaded',
                            'URL preview'
                        );
                    }
                );

                document.getElementById('aoi_file').addEventListener(
                    'change', function() {
                        uploadedFile = this.files[0] || null;
                        if (!uploadedFile) {
                            clearPreview();
                            setAoiStatus('', false);
                            return;
                        }
                        var name = uploadedFile.name.toLowerCase();
                        if (name.endsWith('.geojson') || name.endsWith('.json')) {
                            var reader = new FileReader();
                            reader.onload = function(evt) {
                                try {
                                    var data = JSON.parse(evt.target.result);
                                    clearPreview();
                                    clearDrawnBox();
                                    previewLayer = L.geoJSON(data, {style: {
                                        color: '#059669',
                                        weight: 2,
                                        fillOpacity: 0.1
                                    }});
                                    previewLayer.addTo(
                                        {{this._parent.get_name()}}
                                    );
                                    {{this._parent.get_name()}}.fitBounds(
                                        previewLayer.getBounds()
                                    );
                                    setAoiStatus(
                                        'File loaded: ' + uploadedFile.name, false
                                    );
                                } catch (err) {
                                    setAoiStatus(
                                        'Invalid GeoJSON: ' + err.message, true
                                    );
                                }
                            };
                            reader.readAsText(uploadedFile);
                        } else if (name.endsWith('.kml')) {
                            setAoiStatus(
                                'Fetching KML preview…', false
                            );
                            var fd = new FormData();
                            fd.append('payload_json', JSON.stringify({
                                aoi: {kind: 'file', value: null}
                            }));
                            fd.append('aoi_file', uploadedFile);
                            fetchAoiPreview(
                                {method: 'POST', body: fd},
                                'KML preview loaded: ' + uploadedFile.name,
                                'KML preview'
                            );
                        } else {
                            setAoiStatus('Unsupported file type', true);
                        }
                    }
                );

                // --- Submit Payload ---
                function buildAoi() {
                    var mode = currentAoiMode();
                    if (mode === 'draw') {
                        if (!currentBbox) {
                            setAoiStatus(
                                'Draw a rectangle, or switch input mode', true
                            );
                            return null;
                        }
                        return {kind: 'draw', value: currentBbox};
                    }
                    if (mode === 'coords') {
                        var la1 = parseFloat(
                            document.getElementById('aoi_lat_min').value
                        );
                        var la2 = parseFloat(
                            document.getElementById('aoi_lat_max').value
                        );
                        var lo1 = parseFloat(
                            document.getElementById('aoi_lon_min').value
                        );
                        var lo2 = parseFloat(
                            document.getElementById('aoi_lon_max').value
                        );
                        if ([la1, la2, lo1, lo2].some(isNaN)) {
                            setAoiStatus('Enter 4 numeric coordinates', true);
                            return null;
                        }
                        return {kind: 'coords', value: {
                            lat_min: la1, lat_max: la2,
                            lon_min: lo1, lon_max: lo2
                        }};
                    }
                    if (mode === 'wkt') {
                        var w = document.getElementById('aoi_wkt').value.trim();
                        if (!/^(POINT|POLYGON)/i.test(w)) {
                            setAoiStatus(
                                'WKT must start with POINT or POLYGON', true
                            );
                            return null;
                        }
                        return {kind: 'wkt', value: w};
                    }
                    if (mode === 'url') {
                        var u = document.getElementById('aoi_url').value.trim();
                        if (!/^https:\/\//i.test(u)) {
                            setAoiStatus('URL must start with https://', true);
                            return null;
                        }
                        return {kind: 'url', value: u};
                    }
                    if (mode === 'file') {
                        if (!uploadedFile) {
                            setAoiStatus('Choose a file', true);
                            return null;
                        }
                        return {kind: 'file', value: null};
                    }
                    return null;
                }

                document.getElementById('action-btn').onclick = function() {
                    var aoi = buildAoi();
                    if (!aoi) return;

                    var payload = {
                        aoi: aoi,
                        search_type: getMultiValues('func-list', 'all'),
                        products: getMultiValues('prod-list', 'all'),

                        // Next Pass Payload
                        satellites: getMultiValues('sat-list', 'all'),
                        np_lookback: document.getElementById('np_lookback').value,
                        drcs: document.getElementById('drcs_enabled').value,
                        event_date: document.getElementById('drcs_event_date').value,

                        // Disasters Payload
                        dis_action: document.getElementById('dis_action').value,
                        dis_date_strat: document.querySelector(
                            'input[name="date_strat"]:checked'
                        ).value,
                        dis_recent_n: document.getElementById('dis_recent_n').value,
                        dis_single_date: document.getElementById(
                            'dis_single_date'
                        ).value,
                        dis_start_date: document.getElementById('dis_start_date').value,
                        dis_end_date: document.getElementById('dis_end_date').value,

                        // Disasters Advanced Opts
                        opt_nomask: document.getElementById('opt_nomask').checked,
                        opt_rc: document.getElementById('opt_rc').checked,
                        opt_cloud: document.getElementById('opt_cloud').checked,
                        opt_fd: document.getElementById('opt_fd').value,
                        opt_st: document.getElementById('opt_st').value
                    };

                    // Populate lat_min/… for draw & coords (backwards compat)
                    if (aoi.kind === 'draw' || aoi.kind === 'coords') {
                        payload.lat_min = aoi.value.lat_min;
                        payload.lat_max = aoi.value.lat_max;
                        payload.lon_min = aoi.value.lon_min;
                        payload.lon_max = aoi.value.lon_max;
                    }

                    var request;
                    if (aoi.kind === 'file') {
                        var fd = new FormData();
                        fd.append('payload_json', JSON.stringify(payload));
                        fd.append('aoi_file', uploadedFile);
                        request = fetch('/process_bbox', {
                            method: 'POST', body: fd
                        });
                    } else {
                        request = fetch('/process_bbox', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify(payload)
                        });
                    }

                    request
                        .then(function(r) {
                            return r.json().then(function(body) {
                                return {ok: r.ok, body: body};
                            });
                        })
                        .then(function(resp) {
                            if (!resp.ok) {
                                setAoiStatus(
                                    resp.body.error || 'Request failed', true
                                );
                                return;
                            }
                            if (resp.body.run_id) {
                                showProgressOverlay();
                                checkStatus(resp.body.run_id);
                            }
                        })
                        .catch(function(e) {
                            setAoiStatus('Error: ' + e.message, true);
                        });
                };

                // --- Progress overlay ------------------------------------
                var progressStartTs = 0;

                function showProgressOverlay() {
                    var el = document.getElementById('progress-overlay');
                    el.classList.add('visible');
                    progressStartTs = Date.now();
                    document.getElementById('progress-stage').textContent =
                        'Starting…';
                    document.getElementById('progress-count').textContent = '';
                    document.getElementById('progress-elapsed').textContent =
                        '00:00';
                    var bar = document.getElementById('progress-bar-inner');
                    bar.classList.add('indeterminate');
                    bar.style.width = '';
                }
                function hideProgressOverlay() {
                    document.getElementById('progress-overlay')
                        .classList.remove('visible');
                }
                function pad2(n) { return (n < 10 ? '0' : '') + n; }
                function fmtElapsed(secs) {
                    secs = Math.max(0, Math.floor(secs));
                    return pad2(Math.floor(secs / 60)) + ':' + pad2(secs % 60);
                }
                function renderProgress(status) {
                    document.getElementById('progress-elapsed').textContent =
                        fmtElapsed((Date.now() - progressStartTs) / 1000);
                    document.getElementById('progress-stage').textContent =
                        status.stage || 'Working…';
                    var bar = document.getElementById('progress-bar-inner');
                    var countEl = document.getElementById('progress-count');
                    var p = status.progress;
                    if (p && p.total > 0) {
                        bar.classList.remove('indeterminate');
                        var pct = Math.min(
                            100, Math.round(100 * p.current / p.total)
                        );
                        bar.style.width = pct + '%';
                        countEl.textContent =
                            p.current + ' / ' + p.total +
                            '  (' + pct + ' %)';
                    } else {
                        bar.classList.add('indeterminate');
                        bar.style.width = '';
                        countEl.textContent = '';
                    }
                }

                function checkStatus(runId) {
                    var encId = encodeURIComponent(runId);
                    fetch('/processing_status?run_id=' + encId)
                        .then(function(r) { return r.json(); })
                        .then(function(status) {
                            if (status.running) {
                                renderProgress(status);
                                setTimeout(
                                    function() { checkStatus(runId); }, 500
                                );
                            } else if (status.error) {
                                hideProgressOverlay();
                                alert('Failed: ' + status.error);
                            } else {
                                hideProgressOverlay();
                                window.location.href =
                                    '/show_maps?run_id=' + encId;
                            }
                        })
                        .catch(function(e) {
                            hideProgressOverlay();
                            alert('Status check failed: ' + e.message);
                        });
                }
                {% endmacro %}
            """
            )

    output_file = file_dir / "activated_events_map.html"

    US_CENTER = [39.8283, -98.5795]
    map_object = folium.Map(location=US_CENTER, zoom_start=5, tiles=None)

    # Add base layers
    folium.TileLayer("Esri.WorldImagery", name="Satellite").add_to(map_object)
    carto_tiles_url = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
    carto_api_key = os.environ.get("CARTO_API_KEY")
    if carto_api_key:
        carto_tiles_url += f"?key={carto_api_key}"
    folium.TileLayer(
        tiles=carto_tiles_url,
        attr=(
            '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
            ' contributors &copy; <a href="https://carto.com/attributions">CARTO</a>'
        ),
        name="Street Map",
        max_zoom=20,
    ).add_to(map_object)

    # Add grouped event layers
    for event_type, group_events in events.items():
        color = _color_from_event_type(event_type)
        # Use circle for earthquakes, rectangle for other events
        family = _detect_family(event_type)
        border_radius = "50%" if family == "earthquake" else "0"
        color_box = (
            "<span style='display:inline-block; width:12px; height:12px; "
            f"background:{color}; margin-right:6px; border:1px solid #333; "
            f"border-radius:{border_radius};'></span>"
        )
        legend_label = f"{color_box}{event_type} ({len(group_events)})"
        feature_group = folium.FeatureGroup(name=legend_label, show=True)

        for e in group_events:
            geom = e.get("aoi_polygon")
            if geom is None:
                log.debug("Event %s has no AOI geometry", e.get("id"))
                continue
            provider = str(e.get("provider", "")).upper()
            props = e.get("properties") if isinstance(e.get("properties"), dict) else {}
            mag = props.get("mag")
            depth = props.get("depth_km")
            # Extract location for earthquake events
            if family == "earthquake":
                # Use the complete place string from properties
                place = props.get("place")
                location_str = html.escape(str(place)) if place else ""
                popup_rows = [
                    ("Provider", provider),
                    ("Severity", e.get("severity")),
                    ("Location", location_str),
                ]
            else:
                popup_rows = [
                    ("Provider", provider),
                    ("Severity", e.get("severity")),
                    ("Description", e.get("title")),
                ]
            if mag is not None:
                try:
                    popup_rows.append(("Magnitude", f"M {float(mag):.1f}"))
                except (TypeError, ValueError):
                    pass
            if depth is not None:
                try:
                    popup_rows.append(("Depth", f"{float(depth):.1f} km"))
                except (TypeError, ValueError):
                    pass
            popup_html = "<br>".join(
                f"<b>{label}:</b> {value}" for label, value in popup_rows if value
            )
            if family == "earthquake":
                header_parts: list[str] = []
                # Extract region (last part after comma) from place
                place = props.get("place")
                if place:
                    place_str = str(place)
                    region = (
                        place_str.split(",")[-1].strip()
                        if "," in place_str
                        else place_str
                    )
                    header_parts.append(html.escape(region))
                if mag is not None:
                    try:
                        header_parts.append(f"M {float(mag):.1f}")
                    except (TypeError, ValueError):
                        pass
                time_ms = props.get("time")
                if time_ms is not None:
                    try:
                        dt = datetime.fromtimestamp(
                            float(time_ms) / 1000, tz=timezone.utc
                        )
                        header_parts.append(dt.strftime("%Y-%m-%d %H:%M UTC"))
                    except (TypeError, ValueError, OSError, OverflowError):
                        pass
                if header_parts:
                    header_html = f"<b>{' &middot; '.join(header_parts)}</b>"
                    popup_html = (
                        f"{header_html}<br>{popup_html}" if popup_html else header_html
                    )
            if isinstance(geom, Point):
                try:
                    radius = _magnitude_to_radius(
                        float(mag) if mag is not None else None
                    )
                except (TypeError, ValueError):
                    radius = _magnitude_to_radius(None)
                folium.CircleMarker(
                    location=[geom.y, geom.x],
                    radius=radius,
                    color=color,
                    weight=1.5,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.55,
                    popup=folium.Popup(popup_html, max_width=350),
                ).add_to(feature_group)
                continue
            if isinstance(geom, MultiPolygon):
                geometries = geom.geoms
            else:
                geometries = [geom]
            for g in geometries:
                GeoJson(
                    data=g.__geo_interface__,
                    style_function=lambda _, c=color: {
                        "color": c,
                        "weight": 2,
                        "fillColor": c,
                        "fillOpacity": 0.35,
                    },
                    highlight_function=lambda _: {"weight": 3, "fillOpacity": 0.6},
                    popup=folium.Popup(popup_html, max_width=350),
                ).add_to(feature_group)
        feature_group.add_to(map_object)

    # Add Layer controls
    folium.LayerControl(collapsed=False).add_to(map_object)

    draw = Draw(
        # Folium otherwise binds an alert() of the raw GeoJSON to every shape
        # it creates, which fires when clicking the placed point.
        show_geometry_on_click=False,
        draw_options={
            "rectangle": True,
            "polygon": False,
            "circle": False,
            "marker": True,
            "polyline": False,
            "circlemarker": False,
        },
        edit_options={"edit": True},
    )
    draw.add_to(map_object)

    # After adding draw controls, add the new dashboard
    map_object.add_child(MapDashboardJS())

    # Save HTML
    map_object.save(output_file)
    log.info("Event map written to %s", output_file)


def _bbox_to_geometry(bbox, timestamp_dir):
    from shapely import Point, wkt
    from shapely.geometry import box

    if isinstance(bbox, str):
        bbox_clean = bbox.strip()
        bbox_upper = bbox_clean.upper()
        if bbox_upper.startswith(("POINT", "POLYGON")):
            geometry = wkt.loads(bbox_clean)
        else:
            # if URL, download
            if _is_url(bbox_clean):
                filename = "AOI_from_url.geojson"
                file_path = Path(timestamp_dir) / filename
                bbox_path = _download_url_to_file(bbox_clean, file_path)
            else:
                raise ValueError(
                    "Local file paths are not allowed for event AOI sources"
                )
            geometry = _geometry_from_file(bbox_path)
    else:
        lat_min, lat_max, lon_min, lon_max = bbox
        if lat_min == lat_max and lon_min == lon_max:
            geometry = Point(lon_min, lat_min)
        else:
            geometry = box(lon_min, lat_min, lon_max, lat_max)

    return geometry, geometry.bounds, geometry.centroid


def _add_aoi_to_events(
    events: Iterable[Event],
    file_dir: str,
) -> List[Event]:
    """
    Enrich events with AOI geometry derived from their link.
    Adds:
      - event["aoi_polygon"]
      - event["aoi"]
      - event["centroid"]
    """
    from shapely.geometry import Point

    out: List[Event] = []
    for e in events:
        props = e.get("properties")
        if not isinstance(props, dict):
            props = {}
        event_type = str(props.get("event") or "")
        link = ""
        event_lower = event_type.lower()
        if "earthquake" in event_lower:
            geom = e.get("geometry")
            coords = geom.get("coordinates") if isinstance(geom, dict) else None
            if isinstance(coords, list) and len(coords) >= 2:
                pt = Point(float(coords[0]), float(coords[1]))
                e["aoi_polygon"] = pt
                e["aoi"] = pt.bounds
                e["centroid"] = pt
            else:
                log.debug("Earthquake %s missing point geometry", e.get("id"))
            out.append(e)
            continue
        if "flood" in event_lower:
            raw_link = e.get("link")
            link = str(raw_link).strip() if isinstance(raw_link, str) else ""
        elif "storm" in event_lower:
            affected_zones = props.get("affectedZones", [])
            link = str(affected_zones[0]) if affected_zones else ""
        if not link:
            log.debug("Event %s has no link; skipping AOI", e.get("id"))
            out.append(e)
            continue

        try:
            aoi_polygon, aoi, centroid = _bbox_to_geometry(link, file_dir)

            e["aoi_polygon"] = aoi_polygon
            e["aoi"] = aoi
            e["centroid"] = centroid

        except Exception as exc:
            log.warning(
                "Failed to build AOI for event %s (link=%r): %s",
                e.get("id"),
                link,
                exc,
            )
        out.append(e)
    return out


def _download_url_to_file(
    url: str,
    output_path: str | Path,
    timeout: int = 30,
    ensure_geojson: bool = True,
) -> Path:
    """
    Download a URL and save its content to a file (GeoJSON-safe).
    """
    output_path = Path(output_path)

    if ensure_geojson and output_path.suffix.lower() != ".geojson":
        output_path = output_path.with_suffix(".geojson")

    _validate_remote_url(url)

    response = requests.get(url, timeout=timeout, stream=True)
    response.raise_for_status()
    ctype = (response.headers.get("Content-Type") or "").lower()
    if "json" not in ctype:
        raise ValueError(f"Expected JSON payload from {url}, got Content-Type={ctype}")

    payload = bytearray()
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > MAX_GEOJSON_BYTES:
            raise ValueError(
                f"Response from {url} exceeded max size ({MAX_GEOJSON_BYTES} bytes)"
            )

    # Parse JSON to ensure validity
    try:
        data = json.loads(payload.decode("utf-8"))
    except ValueError as e:
        raise ValueError(f"Response from {url} is not valid JSON") from e

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return output_path


def _geometry_from_file(path: str | Path):
    """
    Read a geometry from a spatial file (KML or GeoJSON).
    """
    from shapely.geometry import shape
    from shapely.ops import unary_union

    path = Path(path)
    suffix = path.suffix.lower()

    # ---- KML ----
    if suffix == ".kml":
        # Reuse next_pass's KML parser (installed as top-level `utils.utils`).
        # Lazy import so bare envs without next_pass can still import this module.
        from utils.utils import create_polygon_from_kml

        return create_polygon_from_kml(str(path))

    # ---- GeoJSON ----
    if suffix in (".geojson", ".json"):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # FeatureCollection
        if data["type"] == "FeatureCollection":
            geometries = [shape(f["geometry"]) for f in data["features"]]
            return geometries[0] if len(geometries) == 1 else unary_union(geometries)

        # Single Feature
        if data.get("type") == "Feature":
            geometry = data.get("geometry")

            if geometry is None:
                affected = data.get("properties", {}).get("affectedZones", [])

                geometries = []
                for zone_url in affected:
                    try:
                        _validate_remote_url(zone_url)
                        resp = requests.get(zone_url, timeout=30)
                        resp.raise_for_status()
                        if len(resp.content) > MAX_GEOJSON_BYTES:
                            raise ValueError(
                                f"Response from {zone_url} exceeded max size "
                                f"({MAX_GEOJSON_BYTES} bytes)"
                            )
                        zone_data = resp.json()
                        if zone_data.get("geometry"):
                            geometries.append(shape(zone_data["geometry"]))
                    except Exception as exc:
                        log.debug(
                            "affectedZones fetch failed for %s: %s", zone_url, exc
                        )

                if geometries:
                    return (
                        geometries[0]
                        if len(geometries) == 1
                        else unary_union(geometries)
                    )
                else:
                    raise ValueError("No geometry found and affectedZones failed.")

            return shape(geometry)

        # Raw geometry
        return shape(data)

    raise ValueError(f"Unsupported spatial file format: {path}")

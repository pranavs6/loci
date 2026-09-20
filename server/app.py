#!/usr/bin/env python3
"""loci web — phone-friendly control page for the loci CLI.

Runs on the Mac. The iPhone keeps Wi-Fi while cabled, so it can reach this
over the LAN and change location without touching the laptop.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

LOCI = os.environ.get("LOCI_BIN") or shutil.which("loci") or str(
    Path.home() / ".local/bin/loci"
)
TOKEN = os.environ.get("LOCI_TOKEN", "")
PRESETS = [
    ("london", "London"),
    ("bangalore", "Bangalore"),
    ("mumbai", "Mumbai"),
    ("delhi", "Delhi"),
    ("nyc", "New York"),
    ("sf", "San Francisco"),
    ("tokyo", "Tokyo"),
]


def run(*args: str) -> tuple[bool, str]:
    try:
        p = subprocess.run(
            [LOCI, *args], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        return False, "timed out"
    out = (p.stdout + p.stderr).strip()
    return p.returncode == 0, out


def authorized() -> bool:
    if not TOKEN:
        return True
    return request.args.get("t") == TOKEN or request.headers.get("X-Loci-Token") == TOKEN


@app.before_request
def _gate():
    if not authorized():
        return jsonify(ok=False, output="unauthorized"), 401


@app.get("/")
def index():
    return render_template("index.html", presets=PRESETS, token=TOKEN)


@app.get("/status")
def status():
    ok, out = run("status")
    return jsonify(ok=ok, output=out, spoofed=out.startswith("SPOOFED"))


@app.post("/set")
def set_location():
    data = request.get_json(silent=True) or {}
    preset = (data.get("preset") or "").strip()
    if preset:
        ok, out = run("set", preset)
    else:
        lat = str(data.get("lat", "")).strip()
        lon = str(data.get("lon", "")).strip()
        if not lat or not lon:
            return jsonify(ok=False, output="need a preset or lat/lon"), 400
        ok, out = run("set", lat, lon)
    return jsonify(ok=ok, output=out)


@app.get("/search")
def search():
    """Proxy Nominatim so the lookup carries a real User-Agent, per OSM policy."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 6, "addressdetails": 0}
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "loci/1.0 (personal GPS tool)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
    except Exception as exc:  # noqa: BLE001 - surface any lookup failure to the UI
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        [
            {"name": d.get("display_name"), "lat": float(d["lat"]), "lon": float(d["lon"])}
            for d in data
        ]
    )


@app.post("/clear")
def clear():
    ok, out = run("clear")
    return jsonify(ok=ok, output=out)


if __name__ == "__main__":
    app.run(
        host=os.environ.get("LOCI_HOST", "0.0.0.0"),
        port=int(os.environ.get("LOCI_PORT", "8787")),
        threaded=True,
    )

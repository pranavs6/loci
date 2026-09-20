#!/usr/bin/env python3
"""loci web — phone-friendly control page for the loci CLI.

Runs on the Mac; the phone drives it over the LAN. Sessions live in SQLite
(see auth.py). Binding a non-loopback address without an account is refused,
because this endpoint can move your phone's location.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

import auth

app = Flask(__name__)

LOCI = os.environ.get("LOCI_BIN") or shutil.which("loci") or str(Path.home() / ".local/bin/loci")
COOKIE = "loci_session"
OPEN_PATHS = {"/login", "/healthz"}
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
        p = subprocess.run([LOCI, *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def client_ip() -> str:
    return request.remote_addr or "?"


@app.before_request
def _gate():
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    uid = auth.validate_session(request.cookies.get(COOKIE, ""))
    if uid is None:
        # Only the page itself redirects. `Accept: */*` satisfies accept_html,
        # so sniffing the header would bounce XHR calls too and the UI would
        # never see a 401 to act on.
        if request.path == "/" and request.method == "GET":
            return redirect(url_for("login"))
        return jsonify(ok=False, output="not authenticated"), 401
    g.user_id = uid
    return None


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)
    ip = client_ip()
    if auth.locked_out(ip):
        return render_template("login.html", error="Too many attempts. Wait 15 minutes."), 429
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    uid = auth.verify_user(username, password, ip)
    if uid is None:
        auth.log(None, "login_failed", username, ip)
        return render_template("login.html", error="Wrong username or password."), 401
    token = auth.create_session(uid, request.headers.get("User-Agent", ""))
    auth.log(uid, "login", "", ip)
    resp = redirect(url_for("index"))
    resp.set_cookie(
        COOKIE, token, httponly=True, samesite="Lax", max_age=auth.SESSION_DAYS * 86400, path="/"
    )
    return resp


@app.post("/logout")
def logout():
    token = request.cookies.get(COOKIE, "")
    auth.log(getattr(g, "user_id", None), "logout", "", client_ip())
    auth.delete_session(token)
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/")
def index():
    return render_template(
        "index.html", presets=PRESETS, username=auth.username_for(g.user_id)
    )


@app.get("/status")
def status():
    ok, out = run("status")
    return jsonify(ok=ok, output=out, spoofed=out.startswith("SPOOFED"))


@app.get("/audit")
def audit():
    return jsonify(auth.recent_audit())


@app.get("/search")
def search():
    """Proxy Nominatim so the lookup carries a real User-Agent, per OSM policy."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "limit": 6, "addressdetails": 0}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "loci/1.0 (personal GPS tool)"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
    except Exception as exc:  # noqa: BLE001 - surface any lookup failure to the UI
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        [{"name": d.get("display_name"), "lat": float(d["lat"]), "lon": float(d["lon"])} for d in data]
    )


@app.post("/set")
def set_location():
    data = request.get_json(silent=True) or {}
    preset = (data.get("preset") or "").strip()
    if preset:
        if preset not in {k for k, _ in PRESETS}:
            return jsonify(ok=False, output="unknown preset"), 400
        ok, out = run("set", preset)
        detail = preset
    else:
        # Round-trip through float: the CLI interpolates coords unquoted, so a
        # malformed value would word-split into stray arguments.
        try:
            lat = float(data.get("lat"))
            lon = float(data.get("lon"))
        except (TypeError, ValueError):
            return jsonify(ok=False, output="lat/lon must be numbers"), 400
        if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            return jsonify(ok=False, output="lat/lon out of range"), 400
        ok, out = run("set", f"{lat:.6f}", f"{lon:.6f}")
        detail = f"{lat:.6f},{lon:.6f}"
    auth.log(g.user_id, "set" if ok else "set_failed", detail, client_ip())
    return jsonify(ok=ok, output=out)


@app.post("/clear")
def clear():
    ok, out = run("clear")
    auth.log(g.user_id, "clear" if ok else "clear_failed", "", client_ip())
    return jsonify(ok=ok, output=out)


def _cli() -> int:
    """Account management: app.py adduser|deluser|users|revoke"""
    auth.init_db()
    cmd = sys.argv[1]
    if cmd == "adduser":
        import getpass

        name = sys.argv[2] if len(sys.argv) > 2 else input("username: ").strip()
        pw = getpass.getpass("password: ")
        if pw != getpass.getpass("confirm: "):
            print("passwords do not match", file=sys.stderr)
            return 1
        try:
            auth.create_user(name, pw)
        except Exception as exc:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"created {name}")
    elif cmd == "deluser":
        print("deleted" if auth.delete_user(sys.argv[2]) else "no such user")
    elif cmd == "users":
        for u in auth.list_users():
            print(u)
    elif cmd == "revoke":
        print(f"revoked {auth.revoke_all_sessions()} session(s)")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(_cli())
    auth.init_db()
    host = os.environ.get("LOCI_HOST", "0.0.0.0")
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth.has_users():
        raise SystemExit(
            "refusing to bind %s with no accounts — create one first:\n"
            "  python server/app.py adduser <name>\n"
            "(or set LOCI_HOST=127.0.0.1 for loopback-only)" % host
        )
    app.run(host=host, port=int(os.environ.get("LOCI_PORT", "8787")), threaded=True)

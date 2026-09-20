#!/usr/bin/env python3
"""loci web — phone-friendly control page for the loci CLI.

Runs on the Mac; the phone drives it over the LAN. Sessions and saved
locations live in SQLite. Binding a non-loopback address without an account
is refused, because this endpoint can move your phone's location.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

from flask import Flask, g, jsonify, redirect, render_template, request, url_for

import auth
import store

app = Flask(__name__)

LOCI = os.environ.get("LOCI_BIN") or shutil.which("loci") or str(Path.home() / ".local/bin/loci")
COOKIE = "loci_session"
OPEN_PATHS = {"/login", "/healthz", "/favicon.ico"}
JSON_PATHS = {"/status", "/search", "/reverse", "/audit", "/set", "/clear"}


def run(*args: str) -> tuple[bool, str]:
    try:
        p = subprocess.run([LOCI, *args], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def client_ip() -> str:
    return request.remote_addr or "?"


def coords(data: dict) -> tuple[float, float]:
    """The CLI interpolates coords unquoted, so anything malformed would
    word-split into stray arguments. Round-trip through float here."""
    try:
        lat, lon = float(data.get("lat")), float(data.get("lon"))
    except (TypeError, ValueError):
        raise ValueError("lat/lon must be numbers") from None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("lat/lon out of range")
    return lat, lon


@app.before_request
def _gate():
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    uid = auth.validate_session(request.cookies.get(COOKIE, ""))
    if uid is None:
        # Only the page itself redirects. `Accept: */*` satisfies accept_html,
        # so sniffing the header would bounce XHR calls too and the UI would
        # never see a 401 to act on.
        if (
            request.method == "GET"
            and not request.path.startswith("/api/")
            and request.path not in JSON_PATHS
        ):
            return redirect(url_for("login"))
        return jsonify(ok=False, output="not authenticated"), 401
    g.user_id = uid
    return None


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.svg")


@app.get("/healthz")
def healthz():
    return jsonify(ok=True)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, username=None, active=None)
    ip = client_ip()
    if auth.locked_out(ip):
        return render_template("login.html", error="Too many sign-in attempts. Try again in 15 minutes.", username=None, active=None), 429
    username = (request.form.get("username") or "").strip()
    uid = auth.verify_user(username, request.form.get("password") or "", ip)
    if uid is None:
        auth.log(None, "login_failed", username, ip)
        return render_template("login.html", error="The username or password is incorrect.", username=None, active=None), 401
    store.seed_defaults(uid)
    token = auth.create_session(uid, request.headers.get("User-Agent", ""))
    auth.log(uid, "login", "", ip)
    resp = redirect(url_for("index"))
    resp.set_cookie(COOKIE, token, httponly=True, samesite="Lax",
                    max_age=auth.SESSION_DAYS * 86400, path="/")
    return resp


@app.post("/logout")
def logout():
    auth.log(getattr(g, "user_id", None), "logout", "", client_ip())
    auth.delete_session(request.cookies.get(COOKIE, ""))
    resp = redirect(url_for("login"))
    resp.delete_cookie(COOKIE, path="/")
    return resp


def _nav(active: str) -> dict:
    return {"active": active, "username": auth.username_for(g.user_id)}


@app.get("/")
def index():
    return render_template("index.html", **_nav("set"))


@app.get("/locations")
def locations_page():
    return render_template(
        "locations.html", locations=store.list_locations(g.user_id), **_nav("locations")
    )


@app.get("/locations/new")
def location_new():
    return render_template("location_form.html", location=None, **_nav("locations"))


@app.get("/locations/<int:loc_id>/edit")
def location_edit(loc_id: int):
    loc = store.get_location(g.user_id, loc_id)
    if loc is None:
        return redirect(url_for("locations_page"))
    return render_template("location_form.html", location=loc, **_nav("locations"))


@app.route("/locations/<int:loc_id>/delete", methods=["GET", "POST"])
def location_delete(loc_id: int):
    loc = store.get_location(g.user_id, loc_id)
    if loc is None:
        return redirect(url_for("locations_page"))
    if request.method == "GET":
        return render_template("delete_location.html", location=loc, **_nav("locations"))
    store.soft_delete(g.user_id, loc_id)
    auth.log(g.user_id, "location_delete", loc["name"], client_ip())
    return redirect(url_for("locations_page"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    info = auth.user_info(g.user_id)
    notice = error = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "password":
            try:
                auth.change_password(
                    g.user_id,
                    request.form.get("current") or "",
                    request.form.get("new") or "",
                )
            except ValueError as exc:
                error = str(exc)
            else:
                # Keep this session; drop the rest, since the password changed.
                auth.revoke_user_sessions(g.user_id, keep=request.cookies.get(COOKIE, ""))
                auth.log(g.user_id, "password_change", "", client_ip())
                notice = "Your password has been changed. Other devices were signed out."
        elif action == "revoke":
            n = auth.revoke_user_sessions(g.user_id, keep=request.cookies.get(COOKIE, ""))
            auth.log(g.user_id, "sessions_revoked", str(n), client_ip())
            notice = f"Signed out of {n} other session(s)."
        info = auth.user_info(g.user_id)
    return render_template(
        "profile.html", info=info, notice=notice, error=error, **_nav("profile")
    )


@app.get("/activity")
def activity_page():
    return render_template("activity.html", rows=auth.recent_audit(), **_nav("activity"))


# `loci status` prints either
#   SPOOFED: <label> (<lat> <lon>) [<link>]
#   not spoofed [<link>]
SPOOFED_RE = re.compile(
    r"^SPOOFED:\s+(?P<label>.*?)\s+\((?P<lat>-?[\d.]+)\s+(?P<lon>-?[\d.]+)\)\s+\[(?P<src>\w+)\]"
)
LINK_RE = re.compile(r"\[(?P<src>\w+)\]\s*$")


@app.get("/status")
def status():
    ok, out = run("status")
    m = SPOOFED_RE.match(out)
    if m:
        lat, lon = float(m["lat"]), float(m["lon"])
        saved = store.find_by_coords(g.user_id, lat, lon)
        return jsonify(
            ok=ok, output=out, spoofed=True, source=m["src"],
            lat=lat, lon=lon,
            name=saved["name"] if saved else None,
            label=m["label"],
        )
    link = LINK_RE.search(out)
    return jsonify(
        ok=ok, output=out, spoofed=False,
        source=link["src"] if link else "unknown",
        lat=None, lon=None, name=None, label=None,
    )


@app.get("/audit")
def audit():
    return jsonify(auth.recent_audit())


# ---------------------------------------------------------------- locations

@app.get("/api/locations")
def api_list():
    return jsonify(store.list_locations(g.user_id, include_deleted=request.args.get("all") == "1"))


@app.post("/api/locations")
def api_create():
    data = request.get_json(silent=True) or {}
    try:
        loc = store.create_location(g.user_id, data.get("name"), data.get("lat"), data.get("lon"))
    except store.StoreError as exc:
        return jsonify(ok=False, output=str(exc)), 400
    auth.log(g.user_id, "location_create", loc["name"], client_ip())
    return jsonify(ok=True, location=loc), 201


@app.patch("/api/locations/<int:loc_id>")
def api_update(loc_id: int):
    data = request.get_json(silent=True) or {}
    try:
        loc = store.update_location(g.user_id, loc_id, data.get("name"), data.get("lat"), data.get("lon"))
    except store.StoreError as exc:
        return jsonify(ok=False, output=str(exc)), 400
    auth.log(g.user_id, "location_update", loc["name"], client_ip())
    return jsonify(ok=True, location=loc)


@app.delete("/api/locations/<int:loc_id>")
def api_delete(loc_id: int):
    if not store.soft_delete(g.user_id, loc_id):
        return jsonify(ok=False, output="no such location"), 404
    auth.log(g.user_id, "location_delete", str(loc_id), client_ip())
    return jsonify(ok=True)


# -------------------------------------------------------------------- set

@app.get("/reverse")
def reverse():
    """Reverse geocode, served from SQLite when we have seen the point before.

    Status polls every 10s and Nominatim asks for roughly 1 req/sec, so an
    in-process cache was not enough: it died with every restart."""
    try:
        lat, lon = coords(request.args)
    except ValueError as exc:
        return jsonify(ok=False, output=str(exc)), 400

    hit = store.cached_address(lat, lon)
    if hit is not None:
        return jsonify(ok=True, address=hit, cached=True)

    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 18}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "loci/1.0 (personal GPS tool)"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
    except Exception as exc:  # noqa: BLE001 - a failure must not be cached
        return jsonify(ok=False, output=str(exc)), 502

    address = data.get("display_name")
    if not address:
        # Genuinely no address here (ocean, desert). Cache it so we do not ask
        # again for the same empty point.
        address = "No address found"
    store.cache_address(lat, lon, address)
    return jsonify(ok=True, address=address, cached=False)


@app.post("/set")
def set_location():
    data = request.get_json(silent=True) or {}
    if data.get("id") is not None:
        loc = store.get_location(g.user_id, int(data["id"]))
        if loc is None:
            return jsonify(ok=False, output="no such location"), 404
        lat, lon, saved = loc["lat"], loc["lon"], loc
    else:
        try:
            lat, lon = coords(data)
        except ValueError as exc:
            return jsonify(ok=False, output=str(exc)), 400
        saved = store.find_by_coords(g.user_id, lat, lon)
    ok, out = run("set", f"{lat:.6f}", f"{lon:.6f}")
    auth.log(g.user_id, "set" if ok else "set_failed",
             saved["name"] if saved else f"{lat:.6f},{lon:.6f}", client_ip())
    return jsonify(
        ok=ok, output=out, lat=lat, lon=lon,
        saved=saved["name"] if saved else None,
        known=saved is not None,
    )


@app.post("/clear")
def clear():
    ok, out = run("clear")
    auth.log(g.user_id, "clear" if ok else "clear_failed", "", client_ip())
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
    req = urllib.request.Request(url, headers={"User-Agent": "loci/1.0 (personal GPS tool)"})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            data = json.load(r)
    except Exception as exc:  # noqa: BLE001 - surface any lookup failure to the UI
        return jsonify({"error": str(exc)}), 502
    return jsonify(
        [{"name": d.get("display_name"), "lat": float(d["lat"]), "lon": float(d["lon"])} for d in data]
    )


def _cli() -> int:
    """Account management: app.py adduser|deluser|users|revoke"""
    auth.init_db()
    store.init_db()
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
        uid = auth.verify_user(name, pw, "cli")
        n = store.seed_defaults(uid) if uid else 0
        print(f"created {name} ({n} default locations)")
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
    store.init_db()
    host = os.environ.get("LOCI_HOST", "0.0.0.0")
    if host not in {"127.0.0.1", "localhost", "::1"} and not auth.has_users():
        raise SystemExit(
            "refusing to bind %s with no accounts — create one first:\n"
            "  python server/app.py adduser <name>\n"
            "(or set LOCI_HOST=127.0.0.1 for loopback-only)" % host
        )
    app.run(host=host, port=int(os.environ.get("LOCI_PORT", "8787")), threaded=True)

// MapLibre v6 is ESM with named exports only - there is no default export.
import * as maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@6.10.0/dist/maplibre-gl.mjs";

/* Shared helpers for the loci pages. */

export async function api(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { location.href = "/login"; return null; }
  let d = {};
  try { d = await r.json(); } catch {}
  if (!r.ok) { problem(d.output || ("Request failed (" + r.status + ")")); return null; }
  return d;
}

export function problem(msg) {
  const box = document.getElementById("error");
  const text = document.getElementById("error-text");
  if (!box) { alert(msg); return; }
  text.textContent = msg;
  box.hidden = false;
  window.scrollTo({ top: 0, behavior: "smooth" });
}

export function clearProblem() {
  const box = document.getElementById("error");
  if (box) box.hidden = true;
}

/* GOV.UK has no spinner component, so this is the one bespoke pattern:
   disable, announce with aria-busy, restore the original label after. */
export function busy(btn, on, label) {
  if (on) {
    btn.dataset.label = btn.dataset.label || btn.textContent;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = '<span class="loci-spinner" aria-hidden="true"></span>' + label;
  } else {
    btn.disabled = false;
    btn.removeAttribute("aria-busy");
    btn.textContent = btn.dataset.label || label;
  }
}

/* Vector-tile map with a single draggable pin.

   MapLibre GL + OpenFreeMap: vector tiles render crisply at any zoom and carry
   real label typography, which raster OSM tiles do not. OpenFreeMap needs no
   API key but declares no attribution in its style, so it is added by hand. */

/* Both styles must carry POI layers — shops, amenities, transit — or the map
   reads as an empty street grid. CARTO dark-matter has only stadium/park, and
   OpenFreeMap's own dark style has none at all, so dark uses VersaTiles
   eclipse (9 POI categories) instead. */
const STYLES = {
  light: "https://tiles.openfreemap.org/styles/liberty",
  dark: "https://tiles.versatiles.org/assets/styles/eclipse/style.json",
};
const ATTRIBUTION = {
  light:
    '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> · ' +
    '<a href="https://openmaptiles.org/" target="_blank" rel="noreferrer">OpenMapTiles</a> · ' +
    '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
  dark:
    '<a href="https://versatiles.org" target="_blank" rel="noreferrer">VersaTiles</a> · ' +
    '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a> contributors',
};

/* Remembered per browser; falls back to the OS setting. Storage can throw in
   private windows, so every access is guarded. */
function readTheme() {
  try {
    const saved = localStorage.getItem("loci-theme");
    if (saved === "light" || saved === "dark") return saved;
  } catch {}
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}
function writeTheme(t) {
  try { localStorage.setItem("loci-theme", t); } catch {}
}

/* eclipse draws POI icons but no names: its poi-* layers already declare a
   text-font and simply have no text-field. Add one so shops and amenities read
   like they do on the light style. */
async function loadStyle(theme) {
  const url = STYLES[theme];
  if (theme !== "dark") return url;
  try {
    const style = await (await fetch(url)).json();
    for (const l of style.layers || []) {
      if (l.type !== "symbol" || !l.id.startsWith("poi-")) continue;
      l.layout = Object.assign({}, l.layout, {
        "text-field": ["get", "name"],
        "text-size": 11,
        "text-anchor": "top",
        "text-offset": [0, 0.9],
        "text-optional": true,
        "text-max-width": 9,
      });
      l.paint = Object.assign({}, l.paint, {
        "text-color": "#d8dde3",
        "text-halo-color": "rgba(0,0,0,0.85)",
        "text-halo-width": 1.2,
      });
    }
    return style;
  } catch {
    return url;   // fall back to the unpatched style rather than no map
  }
}

class ThemeControl {
  constructor(theme, onToggle) { this._theme = theme; this._onToggle = onToggle; }
  onAdd() {
    const wrap = document.createElement("div");
    wrap.className = "maplibregl-ctrl maplibregl-ctrl-group";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "loci-theme-btn";
    const label = () => (this._theme === "dark" ? "Use light map" : "Use dark map");
    const glyph = () => (this._theme === "dark" ? "☀" : "☾");
    btn.textContent = glyph();
    btn.title = label();
    btn.setAttribute("aria-label", label());
    btn.onclick = () => {
      this._theme = this._theme === "dark" ? "light" : "dark";
      btn.textContent = glyph();
      btn.title = label();
      btn.setAttribute("aria-label", label());
      this._onToggle(this._theme);
    };
    wrap.appendChild(btn);
    return wrap;
  }
  onRemove() {}
}

export async function createMap(elId, onPick, initial) {
  const start = initial || { lat: 20, lon: 0, zoom: 2 };
  let theme = readTheme();
  const initialStyle = await loadStyle(theme);
  let map;
  try {
    map = new maplibregl.Map({
      container: elId,
      style: initialStyle,
      center: [start.lon, start.lat],   // note: MapLibre is lng,lat
      zoom: start.zoom,
      attributionControl: false,
    });
  } catch (e) {
    document.getElementById(elId).innerHTML =
      '<p class="loci-places__empty">This map needs WebGL, which this browser has turned off.</p>';
    return { map: null, place: () => {} };
  }
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-left");

  // Each provider needs its own credit, so the control is rebuilt on a switch.
  let attrib = new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION[theme] });
  map.addControl(attrib);
  map.addControl(new ThemeControl(theme, async next => {
    theme = next;
    writeTheme(next);
    map.setStyle(await loadStyle(next));  // markers are DOM nodes and survive this
    map.removeControl(attrib);
    attrib = new maplibregl.AttributionControl({ compact: true, customAttribution: ATTRIBUTION[next] });
    map.addControl(attrib);
  }), "top-right");

  let marker = null;
  // `silent` places the pin without firing onPick, so callers that are already
  // reacting to a choice can move the pin without re-entering their own handler.
  function place(lat, lon, fly, silent) {
    lat = +(+lat).toFixed(6); lon = +(+lon).toFixed(6);
    if (marker) {
      marker.setLngLat([lon, lat]);
    } else {
      marker = new maplibregl.Marker({ draggable: true, color: "#1d70b8" })
        .setLngLat([lon, lat])
        .addTo(map);
      marker.on("dragend", () => {
        const p = marker.getLngLat();
        place(p.lat, p.lng, false);
      });
    }
    if (fly) map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), 13) });
    if (!silent) onPick(lat, lon);
  }

  map.on("click", e => place(e.lngLat.lat, e.lngLat.lng, false));
  if (initial && initial.marker) place(start.lat, start.lon, false, true);
  return { map, place };
}

/* Nominatim asks for no more than ~1 request per second, hence the debounce. */
export function attachSearch(input, results, onChoose) {
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const term = input.value.trim();
    if (term.length < 3) { results.style.display = "none"; return; }
    timer = setTimeout(async () => {
      const list = await api("/search?q=" + encodeURIComponent(term));
      if (!Array.isArray(list) || !list.length) { results.style.display = "none"; return; }
      results.innerHTML = "";
      list.forEach(item => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = item.name;
        b.onclick = () => {
          results.style.display = "none";
          input.value = item.name.split(",")[0];
          onChoose(item.lat, item.lon);
        };
        results.appendChild(b);
      });
      results.style.display = "block";
    }, 600);
  });
  document.addEventListener("click", e => {
    if (!e.target.closest(".loci-search")) results.style.display = "none";
  });
}

"""Web UI: enter a seed, generate a layered-drunk terrain map, and view it in the browser.

Serves a single page at ``http://<ui.host>:<ui.port>/`` (``localhost:9000`` by
default) with a seed field, Generate and Export buttons, a progress bar,
vertical-scale and sea sliders, an editor sea-level field and the map, drawn as ``main`` draws it. Generation runs in a
background thread through ``app.pipeline.generate_terrain``; the page polls
for progress and shows the PNG when it is ready. One map is generated at a
time.

A sea slider (0 to ``ui.sea_fraction_max``, default ``sea.water_fraction``)
sets the share of the map that is sea. It applies when Generate is pressed,
since sea level decides where rivers drain to, so it is locked while a map
is generating.

The map is the whole Cities: Skylines II world map (``cs2.world_width_km``
across) with the playable area (``cs2.playable_width_km``) outlined at its
centre. Clicking the map moves the playable area to the clicked point: the
world wraps around, so it is rolled to bring that point to the centre. A
dashed square previews the new position under the mouse.

Heights stay normalised (0-1) until they are labelled or exported in
Cities: Skylines II metres (``app.heights.HeightMapping``): one unit is the
vertical-scale slider's value (default ``ui.vertical_scale_m``, at most
``cs2.max_height_m``), and the model's sea level is put at the editor sea
level typed in (default ``cs2.editor_sea_level_m``), so the coastline in the
game matches the map. Changing either redraws the map's numbers (colour bar,
sea level, title); the picture itself doesn't change.

Uses only the Python standard library's HTTP server, so it needs no extra
dependencies. Endpoints:

- ``GET /``: the page.
- ``POST /api/generate`` with JSON ``{"seed": <int>, "sea_percent": <number>}``
  (``sea_percent`` optional, 0 to ``ui.sea_fraction_max`` x 100): start a job;
  returns ``{"job": <id>}`` (409 if a job is already running).
- ``GET /api/progress/<id>``: ``{"fraction", "message", "done", "error",
  "info"}``; ``info`` (when done) gives the normalised sea level, lowest and
  highest points, the sea fraction, river counts and grid size.
- ``GET /api/export/<id>?kind=<world|playable>&scale=<m>&sl=<m>&cx=<col>&cy=<row>``:
  a Cities: Skylines II heightmap (4096 x 4096, 16-bit PNG, ``app.export``)
  for that view, sent as a download named after its settings.
- ``GET /api/image/<id>?scale=<m>&sl=<m>&cx=<col>&cy=<row>``: the finished
  map as PNG, labelled in metres with vertical scale ``scale`` (default
  ``ui.vertical_scale_m``) and editor sea level ``sl`` (default
  ``cs2.editor_sea_level_m``), rolled so grid cell ``(cx, cy)`` is at the
  centre (default: the middle cell).
"""

import argparse
import io
import itertools
import json
import platform
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import replace
from dataclasses import field as dataclass_field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

# When run as a script (python app/ui.py) rather than a module (python -m app.ui),
# put the project root on sys.path so the `app` package can be imported.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.export import ExportFile
from app.export import export_heightmaps
from app.heights import HeightMapping
from app.pipeline import TerrainResult
from app.pipeline import generate_terrain
from app.rendering import MAP_RECT
from app.rendering import save_terrain_map

# matplotlib's pyplot isn't thread-safe; the server handles requests on several threads.
RENDER_LOCK = threading.Lock()
# Rendered images kept per finished job (one per slider value and centre recently viewed).
IMAGE_CACHE_SIZE = 16
# Resolution of the map images: 7 x 6 in at 140 dpi is 980 x 840 px, the page's width.
UI_DPI = 140

# The single page: seed field, Generate button, progress bar, status line and map.
PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Drunk terrain</title>
<style>
  :root { --bg: #f6f7f9; --panel: #ffffff; --text: #1d232b; --muted: #5d6875; --accent: #2f6fdb; --border: #d8dde3; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #15181c; --panel: #1f242a; --text: #e6e9ed; --muted: #9aa5b1; --accent: #6ea0ff; --border: #343b44; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 15px/1.4 system-ui, sans-serif; }
  main { max-width: 1000px; margin: 0 auto; padding: 24px 16px; }
  h1 { font-size: 20px; margin: 0 0 16px; }
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  label { color: var(--muted); }
  input { width: 140px; padding: 8px 10px; font: inherit; color: var(--text); background: var(--panel);
          border: 1px solid var(--border); border-radius: 6px; }
  button { padding: 8px 16px; font: inherit; color: #fff; background: var(--accent); border: 0;
           border-radius: 6px; cursor: pointer; }
  button:disabled { opacity: 0.5; cursor: default; }
  progress { width: 100%; height: 10px; margin-top: 16px; accent-color: var(--accent); }
  #status { color: var(--muted); min-height: 1.4em; margin-top: 6px; }
  .mapwrap { position: relative; overflow: hidden; margin-top: 16px; border: 1px solid var(--border);
             border-radius: 6px; background: var(--panel); }
  .mapwrap[hidden] { display: none; }
  #map { display: block; width: 100%; height: auto; }
  #map.picking { cursor: crosshair; }
  #preview { position: absolute; border: 2px dashed #fff; outline: 1px solid rgba(0, 0, 0, 0.6);
             pointer-events: none; }
  #preview[hidden] { display: none; }
  .scale { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 14px; }
  .scale input[type=range] { flex: 1; min-width: 180px; width: auto; padding: 0; accent-color: var(--accent); }
  #scaleValue { min-width: 72px; font-variant-numeric: tabular-nums; }
  #sealevel { width: 100px; }
  .hint { color: var(--muted); font-size: 13px; }
</style>
</head>
<body>
<main>
  <h1>Drunk terrain</h1>
  <form class="controls" id="form">
    <label for="seed">Seed</label>
    <input id="seed" type="number" step="1" value="42" required>
    <button id="go" type="submit">Generate</button>
    <button id="export" type="button" disabled>Export heightmaps</button>
  </form>
  <div class="scale">
    <label for="scale">Vertical scale</label>
    <input id="scale" type="range" min="__SCALE_MIN__" max="__SCALE_MAX__" step="__SCALE_STEP__" value="__SCALE_DEFAULT__">
    <span id="scaleValue"></span>
  </div>
  <div class="hint">Metres from the world's lowest to its highest point. __SCALE_DEFAULT__ m matches the slopes of real
    mountain and upland terrain; about 900 m is gentle uplands, 3,500 m the Alps.</div>
  <div class="scale">
    <label for="sealevel">Sea level in the editor</label>
    <input id="sealevel" type="number" min="0" max="2000" step="0.1" value="__SEA_LEVEL_DEFAULT__"> m
  </div>
  <div class="hint">Set this to the map editor's sea level: exported heights are shifted so the coastline sits
    exactly at it. Deep sea floor that would fall below 0 m is flattened at 0 m.</div>
  <div class="scale">
    <label for="sea">Sea</label>
    <input id="sea" type="range" min="0" max="__SEA_MAX__" step="1" value="__SEA_DEFAULT__">
    <span id="seaValue"></span>
  </div>
  <div class="hint" id="seaHint">Share of the map that is sea. Applies when you press Generate (rivers drain to the sea).</div>
  <progress id="bar" max="1" value="0"></progress>
  <div id="status">Enter a seed and press Generate.</div>
  <div class="hint" id="pickHint" hidden>The white square is the playable area (__PLAYABLE_KM__ km) at the centre
    of the __WORLD_KM__ km world map. Click anywhere to move it there: the world wraps around, so the map
    re-centres on your point. <b>Export heightmaps</b> downloads the Cities: Skylines II world map and
    playable-area heightmap (4096 x 4096, 16-bit) for this view, at the vertical scale and editor sea level
    above; both file names record the seed, sea share, centre, vertical scale and sea level.</div>
  <div class="mapwrap" id="mapwrap" hidden>
    <img id="map" alt="Generated terrain map">
    <div id="preview" hidden></div>
  </div>
</main>
<script>
const form = document.getElementById("form"), seedInput = document.getElementById("seed");
const go = document.getElementById("go"), bar = document.getElementById("bar");
const exportButton = document.getElementById("export");
const statusLine = document.getElementById("status"), map = document.getElementById("map");
const scale = document.getElementById("scale"), scaleValue = document.getElementById("scaleValue");
const seaLevel = document.getElementById("sealevel");
const sea = document.getElementById("sea"), seaValue = document.getElementById("seaValue");
const seaHint = document.getElementById("seaHint");
const mapWrap = document.getElementById("mapwrap"), preview = document.getElementById("preview");
const pickHint = document.getElementById("pickHint");
const SEA_HINT = seaHint.textContent;
// Where the map sits in the image (left, bottom, width, height as fractions, from the lower left).
const MAP_RECT = __MAP_RECT__;
const WORLD_KM = __WORLD_KM__, PLAYABLE_KM = __PLAYABLE_KM__;
let generatedSea = null;
let currentJob = null, currentInfo = null, redrawTimer = null;
// Grid cell (column, row) of the generated world shown at the map's centre, inside the playable area.
let centre = null;

const metres = (v) => Math.round(v).toLocaleString() + " m";
function showScale() { scaleValue.textContent = metres(Number(scale.value)); }
// The editor sea level typed in, kept within the editor's 0-2000 m.
function editorSeaLevel() { return Math.min(Math.max(Number(seaLevel.value) || 0, 0), 2000); }
// The query giving the view's vertical scale, editor sea level and centre.
function viewQuery() { return `scale=${scale.value}&sl=${editorSeaLevel()}&cx=${centre[0]}&cy=${centre[1]}`; }
function showSea() {
  seaValue.textContent = sea.value + "%";
  seaHint.textContent = (generatedSea !== null && Number(sea.value) !== generatedSea)
    ? `This map has ${generatedSea}% sea; press Generate to apply ${sea.value}%.` : SEA_HINT;
}
showSea();
sea.addEventListener("input", showSea);
function summary() {
  if (!currentInfo) return;
  const i = currentInfo, sl = editorSeaLevel();
  // Metres as exported: the model's sea level lands on the editor's.
  const inMetres = (h) => sl + (h - i.sea_level) * Number(scale.value);
  let text = `Seed ${i.seed}: sea level ${sl.toLocaleString()} m, highest point ${metres(inMetres(i.max_height))}, `
           + `lowest ${metres(inMetres(i.min_height))}`
           + (inMetres(i.min_height) < 0 ? " (flattened at 0 m in the export)" : "")
           + `, ${Math.round(i.sea_fraction * 100)}% sea`;
  if (i.state === "no sea") text += ", no rivers (with no sea there is nowhere to drain to)";
  else if (i.rivers !== null) text += `, ${i.rivers} rivers (${i.rivers_to_sea} reach the sea)`;
  const km = (c) => ((c + 0.5) * WORLD_KM / i.grid_points).toFixed(1);
  text += `. Playable area centred at ${km(centre[0])}, ${km(centre[1])} km of the generated world.`;
  statusLine.textContent = text;
}
function showMap() {
  map.src = `/api/image/${currentJob}?${viewQuery()}`;
  mapWrap.hidden = false; pickHint.hidden = false;
}

// The point under the mouse as fractions (0-1) across the map from its lower-left corner, or null if off the map.
function mapPoint(event) {
  const r = map.getBoundingClientRect();
  const fx = (event.clientX - r.left) / r.width, fy = 1 - (event.clientY - r.top) / r.height;
  const u = (fx - MAP_RECT[0]) / MAP_RECT[2], v = (fy - MAP_RECT[1]) / MAP_RECT[3];
  return (u >= 0 && u <= 1 && v >= 0 && v <= 1) ? [u, v] : null;
}
map.addEventListener("mousemove", (event) => {
  const point = currentInfo ? mapPoint(event) : null;
  map.classList.toggle("picking", point !== null);
  preview.hidden = point === null;
  if (point === null) return;
  // Outline where the playable area would go, centred on the mouse.
  const r = map.getBoundingClientRect(), side = r.width * MAP_RECT[2] * PLAYABLE_KM / WORLD_KM;
  preview.style.width = preview.style.height = side + "px";
  preview.style.left = (event.clientX - r.left - side / 2) + "px";
  preview.style.top = (event.clientY - r.top - side / 2) + "px";
});
map.addEventListener("mouseleave", () => { preview.hidden = true; });
map.addEventListener("click", (event) => {
  const point = currentInfo ? mapPoint(event) : null;
  if (point === null) return;
  // Move the clicked point to the centre: it lies (point - 0.5) of the way across from the current centre.
  const n = currentInfo.grid_points, wrap = (c) => ((c % n) + n) % n;
  centre = [wrap(centre[0] + Math.round((point[0] - 0.5) * n)), wrap(centre[1] + Math.round((point[1] - 0.5) * n))];
  preview.hidden = true;
  showMap(); summary();
});
showScale();
// Redraw the labels once the vertical scale or sea level settles.
function relabel() {
  showScale(); summary();
  if (currentJob === null || !currentInfo) return;
  clearTimeout(redrawTimer);
  redrawTimer = setTimeout(showMap, 250);
}
scale.addEventListener("input", relabel);
seaLevel.addEventListener("input", relabel);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const seed = parseInt(seedInput.value, 10);
  if (Number.isNaN(seed)) { statusLine.textContent = "The seed must be a whole number."; return; }
  go.disabled = true; sea.disabled = true; exportButton.disabled = true;
  bar.value = 0; statusLine.textContent = "Starting…"; currentInfo = null;
  const seaPercent = Number(sea.value);
  try {
    const response = await fetch("/api/generate", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({seed, sea_percent: seaPercent}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || response.statusText);
    poll(body.job, seed, seaPercent);
  } catch (error) {
    statusLine.textContent = "Could not start: " + error.message; go.disabled = false; sea.disabled = false;
  }
});

function finish() { go.disabled = false; sea.disabled = false; }

// Download the world map and playable-area heightmaps for the current view. The server makes both
// on the first request (a few seconds); the second download follows once the first has arrived.
exportButton.addEventListener("click", async () => {
  if (!currentInfo) return;
  exportButton.disabled = true;
  const query = viewQuery();
  try {
    for (const kind of ["world", "playable"]) {
      statusLine.textContent = `Preparing the ${kind} heightmap…`;
      const response = await fetch(`/api/export/${currentJob}?kind=${kind}&${query}`);
      if (!response.ok) throw new Error((await response.json()).error || response.statusText);
      const name = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition"))[1];
      const link = document.createElement("a");
      link.href = URL.createObjectURL(await response.blob());
      link.download = name;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    }
    summary();
    statusLine.textContent += " Exported both heightmaps.";
  } catch (error) {
    statusLine.textContent = "Export failed: " + error.message;
  }
  exportButton.disabled = false;
});

async function poll(job, seed, seaPercent) {
  try {
    const response = await fetch(`/api/progress/${job}`);
    const state = await response.json();
    bar.value = state.fraction;
    statusLine.textContent = state.message;
    if (state.error) { statusLine.textContent = "Failed: " + state.error; finish(); return; }
    if (state.done) {
      currentJob = job; currentInfo = state.info; generatedSea = seaPercent;
      centre = [Math.floor(state.info.grid_points / 2), Math.floor(state.info.grid_points / 2)];
      map.alt = `Generated terrain map for seed ${seed}`;
      showMap(); summary(); showSea(); exportButton.disabled = false;
      finish(); return;
    }
  } catch (error) {
    statusLine.textContent = "Lost contact with the server: " + error.message; finish(); return;
  }
  setTimeout(() => poll(job, seed, seaPercent), 300);
}
</script>
</body>
</html>
"""


@dataclass
class Job:
    """One generation request and its progress; updated by the worker thread, read by request handlers."""

    seed: int
    water_fraction: float
    # Sides of the world map and of the playable area at its centre, in km.
    world_km: float
    playable_km: float
    fraction: float = 0.0
    message: str = "queued"
    done: bool = False
    error: str | None = None
    result: TerrainResult | None = None
    # Rendered PNGs keyed by (vertical scale m, editor sea level m, centre column, centre row), most recently used last.
    images: OrderedDict = dataclass_field(default_factory=OrderedDict)
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)
    # The last exported heightmaps and the (scale, sea level, cx, cy) they were made for, and the lock that guards them.
    exported: tuple[tuple[float, float, int, int], tuple[ExportFile, ExportFile]] | None = None
    export_lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def update(self, fraction: float, message: str) -> None:
        """Record progress (called from the worker thread)."""
        with self.lock:
            self.fraction = min(max(fraction, 0.0), 1.0)
            self.message = message

    def snapshot(self) -> dict[str, object]:
        """A consistent copy of the job's state for the progress endpoint (with map facts once done)."""
        with self.lock:
            info = None
            if self.result is not None:
                r = self.result
                info = {
                    "seed": self.seed,
                    "sea_level": r.sea_level,
                    "max_height": float(r.field.max()),
                    "min_height": float(r.field.min()),
                    "sea_fraction": r.sea_fraction,
                    "rivers": r.rivers_carved if r.state == "rivers" else None,
                    "state": r.state,
                    "rivers_to_sea": r.rivers_to_sea,
                    "grid_points": r.field.shape[0],
                }
            return {"fraction": self.fraction, "message": self.message, "done": self.done, "error": self.error,
                    "info": info}

    def image(self, scale_m: float, sea_level_m: float, cx: int, cy: int) -> bytes:
        """The finished map as PNG, centred on cell ``(cx, cy)``, labelled in metres as exported (cached).

        Heights are labelled as ``HeightMapping(scale_m, sea_level_m, ...)``
        maps them. The world map wraps around, so it is rolled to bring the
        cell to the centre, where the playable area is outlined.
        """
        key = (round(scale_m, 3), round(sea_level_m, 3), cx, cy)
        with self.lock:
            if key in self.images:
                self.images.move_to_end(key)
                return self.images[key]
            r = self.result
        if r is None:
            raise ValueError("the map isn't finished")
        heights = HeightMapping(scale_m, sea_level_m, r.sea_level)
        title = (f"seed {self.seed}: sea level {sea_level_m:,.1f} m, "
                 f"highest point {heights.metres(float(r.field.max())):,.0f} m, {r.sea_fraction:.0%} sea")
        r = r.centred_on(cx, cy)
        buffer = io.BytesIO()
        with RENDER_LOCK:
            save_terrain_map(buffer, r.field, title, r.sea_level, self.world_km, rivers=r.river_area,
                             heights=heights, playable_km=self.playable_km, dpi=UI_DPI)
        png = buffer.getvalue()
        with self.lock:
            self.images[key] = png
            while len(self.images) > IMAGE_CACHE_SIZE:
                self.images.popitem(last=False)
        return png

    def export(self, config: Config, scale_m: float, sea_level_m: float, cx: int, cy: int) -> tuple[ExportFile, ExportFile]:
        """The world and playable-area heightmaps centred on cell ``(cx, cy)``, at that vertical scale and editor sea level (cached).

        Both are made together and kept for the most recent settings, so the
        page's second download is instant; the export lock stops two requests
        making them twice.
        """
        key = (round(scale_m, 3), round(sea_level_m, 3), cx, cy)
        with self.export_lock:
            if self.exported is None or self.exported[0] != key:
                with self.lock:
                    r = self.result
                if r is None:
                    raise ValueError("the map isn't finished")
                self.exported = (key, export_heightmaps(r, config, round(self.water_fraction * 100, 3), cx, cy,
                                                        scale_m, sea_level_m))
            return self.exported[1]


class JobManager:
    """Runs one generation job at a time in a background thread and keeps finished jobs for viewing."""

    # Finished jobs kept for their images (older ones are dropped).
    KEEP = 10

    def __init__(self, config: Config) -> None:
        """Manage jobs generated with ``config``."""
        self.config = config
        self.page = (
            PAGE.replace("__SCALE_MIN__", f"{config.ui.vertical_scale_min_m:g}")
            .replace("__SCALE_MAX__", f"{config.cs2.max_height_m:g}")
            .replace("__SCALE_STEP__", f"{config.ui.vertical_scale_step_m:g}")
            .replace("__SCALE_DEFAULT__", f"{config.ui.vertical_scale_m:g}")
            .replace("__SEA_LEVEL_DEFAULT__", f"{config.cs2.editor_sea_level_m:g}")
            .replace("__SEA_MAX__", f"{round(config.ui.sea_fraction_max * 100):d}")
            .replace("__SEA_DEFAULT__", f"{round(config.sea.water_fraction * 100):d}")
            .replace("__MAP_RECT__", json.dumps(list(MAP_RECT)))
            .replace("__WORLD_KM__", f"{config.cs2.world_width_km:g}")
            .replace("__PLAYABLE_KM__", f"{config.cs2.playable_width_km:g}")
        )
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._running: Job | None = None

    def start(self, seed: int, water_fraction: float) -> int | None:
        """Start generating ``seed`` with ``water_fraction`` of the map as sea.

        Returns the job id, or None if a job is already running.
        """
        with self._lock:
            if self._running is not None and not self._running.done:
                return None
            job_id = next(self._ids)
            job = Job(seed, water_fraction, self.config.cs2.world_width_km, self.config.cs2.playable_width_km)
            self.jobs[job_id] = job
            self._running = job
            for old in sorted(self.jobs)[: -self.KEEP]:
                del self.jobs[old]
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job_id

    def _run(self, job: Job) -> None:
        """Worker thread: generate the terrain, render it to PNG bytes, and mark the job done."""
        try:
            # The sea fraction is chosen per request; everything else comes from the config file.
            config = replace(self.config, sea=replace(self.config.sea, water_fraction=job.water_fraction))
            result = generate_terrain(config, job.seed, progress=lambda f, m: job.update(0.97 * f, m))
            job.update(0.97, "drawing the map")
            with job.lock:
                job.result = result
            # Draw at the default vertical scale, sea level and centre now, so the first view is instant.
            middle = result.field.shape[0] // 2
            job.image(self.config.ui.vertical_scale_m, self.config.cs2.editor_sea_level_m, middle, middle)
            with job.lock:
                job.fraction, job.message, job.done = 1.0, "done", True
        except Exception as exc:  # report any failure to the page rather than killing the thread silently
            traceback.print_exc()
            with job.lock:
                job.error, job.done = f"{type(exc).__name__}: {exc}", True


def make_handler(manager: JobManager) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to ``manager``."""

    class Handler(BaseHTTPRequestHandler):
        """Serves the page and the JSON/PNG API."""

        def _send(self, status: HTTPStatus, body: bytes, content_type: str, download: str | None = None) -> None:
            """Send a complete response; with ``download``, as a file the browser saves under that name."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if download is not None:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            """Send ``payload`` as JSON."""
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _job(self, prefix: str) -> Job | None:
            """The job named by the path after ``prefix``, or None (and a 404 sent)."""
            try:
                job = manager.jobs.get(int(urlparse(self.path).path[len(prefix):]))
            except ValueError:
                job = None
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
            return job

        def _view(self, job: Job) -> tuple[float, float, int, int] | None:
            """The ``(scale_m, sea_level_m, cx, cy)`` asked for by the query, or None (and an error sent).

            ``scale`` is the vertical scale (default ``ui.vertical_scale_m``,
            kept within the slider's range), ``sl`` the editor sea level
            (default ``cs2.editor_sea_level_m``, 0-2000 m) and ``cx``, ``cy``
            the centre cell (default the middle cell).
            """
            config = manager.config
            query = parse_qs(urlparse(self.path).query)
            try:
                scale_m = float(query.get("scale", [config.ui.vertical_scale_m])[0])
                sea_level_m = float(query.get("sl", [config.cs2.editor_sea_level_m])[0])
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "scale and sl must be numbers of metres"})
                return None
            scale_m = min(max(scale_m, config.ui.vertical_scale_min_m), config.cs2.max_height_m)
            if not 0 <= sea_level_m <= 2000:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "sl (the editor sea level) must be 0-2000 m"})
                return None
            if job.result is None:
                self._json(HTTPStatus.CONFLICT, {"error": "not finished"})
                return None
            n = job.result.field.shape[0]
            try:
                cx = int(query.get("cx", [n // 2])[0])
                cy = int(query.get("cy", [n // 2])[0])
            except ValueError:
                cx = cy = -1
            if not (0 <= cx < n and 0 <= cy < n):
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"cx and cy must be whole numbers from 0 to {n - 1}"})
                return None
            return scale_m, sea_level_m, cx, cy

        def do_GET(self) -> None:
            """Serve the page, job progress, a finished image, or an exported heightmap."""
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, manager.page.encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/progress/"):
                job = self._job("/api/progress/")
                if job is not None:
                    self._json(HTTPStatus.OK, job.snapshot())
            elif self.path.startswith("/api/image/"):
                job = self._job("/api/image/")
                view = self._view(job) if job is not None else None
                if view is not None:
                    self._send(HTTPStatus.OK, job.image(*view), "image/png")
            elif self.path.startswith("/api/export/"):
                job = self._job("/api/export/")
                view = self._view(job) if job is not None else None
                if view is None:
                    return
                kind = parse_qs(urlparse(self.path).query).get("kind", [""])[0]
                if kind not in ("world", "playable"):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "kind must be world or playable"})
                    return
                world, playable = job.export(manager.config, *view)
                chosen = world if kind == "world" else playable
                self._send(HTTPStatus.OK, chosen.png, "image/png", download=chosen.filename)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            """Start a generation job from ``{"seed": <int>}``."""
            if self.path != "/api/generate":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                seed = int(request["seed"])
                sea_percent = float(request.get("sea_percent", manager.config.sea.water_fraction * 100))
            except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST,
                           {"error": "expected JSON {\"seed\": <integer>, \"sea_percent\": <number>}"})
                return
            if seed < 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "the seed must not be negative"})
                return
            max_percent = manager.config.ui.sea_fraction_max * 100
            if not 0 <= sea_percent <= max_percent:
                self._json(HTTPStatus.BAD_REQUEST, {"error": f"sea_percent must be between 0 and {max_percent:g}"})
                return
            job_id = manager.start(seed, sea_percent / 100.0)
            if job_id is None:
                self._json(HTTPStatus.CONFLICT, {"error": "a map is already being generated; please wait"})
            else:
                self._json(HTTPStatus.ACCEPTED, {"job": job_id})

        def log_message(self, format: str, *args: object) -> None:
            """Log only non-polling requests, to keep the console readable."""
            if "/api/progress/" not in self.path:
                super().log_message(format, *args)

    return Handler


def open_in_browser(url: str) -> None:
    """Open ``url`` in a browser; under WSL, use the Windows default browser.

    Inside WSL, Python's ``webbrowser`` usually has no Linux browser to open,
    so the URL is handed to Windows instead (WSL2 forwards localhost).
    """
    if "microsoft" in platform.release().lower():
        try:
            subprocess.run(["explorer.exe", url], check=False, timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    webbrowser.open(url)


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Serve the terrain generator as a web page.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the YAML config file (default: config.yaml at the project root).",
    )
    return parser.parse_args()


def main() -> None:
    """Load config, start the web server, and optionally open the page in a browser."""
    args = _parse_args()
    config = load_config(args.config)
    server = ThreadingHTTPServer((config.ui.host, config.ui.port), make_handler(JobManager(config)))
    url = f"http://{'localhost' if config.ui.host in ('127.0.0.1', '0.0.0.0') else config.ui.host}:{config.ui.port}/"
    print(f"Serving the terrain UI at {url}  (Ctrl+C to stop)")
    if config.ui.open_browser:
        threading.Timer(0.5, lambda: open_in_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

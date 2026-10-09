"""Web UI, the entry point: build a map from a seed, watch it grow, choose the playable area and export CS2 heightmaps.

Run ``python -m app.ui`` and open http://localhost:9000/. One map is built at
a time, in a background thread; the page polls ``/api/state`` and redraws
the map whenever a gang of drunks or plains are accepted. Endpoints:

- ``GET /``: the page.
- ``POST /api/build`` with ``{"seed": int, "trials": int, "sea": percent or null}``: start building with that sea share
  (null: the builder chooses it); 409 while a build runs.
- ``GET /api/state``: build progress, objective history (null until something is kept), statistics, chosen sea
  share, city site and suggested relief, height quantiles.
- ``GET /api/preview.png?sea=&scale=&cx=&cy=``: the map, rolled so cell ``(cx, cy)`` is centred.
- ``GET /api/heightmap/<world|playable>.png?sea=&scale=&sl=&cx=&cy=``: a CS2 heightmap download.
"""

import errno
import json
import platform
import subprocess
import threading
import traceback
import webbrowser
from dataclasses import dataclass
from dataclasses import field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.parse import urlparse

import numpy as np

from app.build_map import REFERENCE
from app.build_map import STAT_NAMES
from app.build_map import TRIALS
from app.build_map import build_map
from app.build_map import measure
from app.build_map import nearest
from app.build_map import suggested_relief
from app.map import EDITOR_SEA_LEVEL_M
from app.map import GRID
from app.map import MAX_HEIGHT_M
from app.map import PLAYABLE_KM
from app.map import SEA_FRACTION
from app.map import VERTICAL_M
from app.map import WORLD_KM
from app.map import Map
from app.map import View

HOST, PORT = "127.0.0.1", 9000


@dataclass
class Build:
    """The current (or last) build and its progress, shared between the build thread and request handlers."""

    seed: int = 0
    map: Map | None = None
    running: bool = False
    trial: int = 0
    trials: int = 0
    history: list[float] = field(default_factory=list)
    accepted: int = 0
    message: str = "Choose a seed and press Build."
    # Per size ("world", "city"): the map's statistics, the mean of its nearest real squares', and their names.
    stats: dict[str, dict[str, list]] = field(default_factory=dict)
    quantiles: list[float] = field(default_factory=list)
    # The builder's choices: share of the map under the sea, the centre cell of the best city site, and metres from
    # lowest to highest point that match the relief of the map's nearest real squares (None if unknown).
    sea_fraction: float = SEA_FRACTION
    site: tuple[int, int] = (GRID // 2, GRID // 2)
    relief: float | None = None
    error: str | None = None


STATE = Build()
LOCK = threading.Lock()


def _snapshot(world: Map) -> tuple[dict[str, dict[str, list]], list[float]]:
    """The map's statistics beside its nearest real squares' (None where undefined), and the percentiles 0-100
    of its heights normalised to [0, 1]."""
    z = world.height
    q = np.percentile(z, np.arange(101))
    q = (q - q[0]) / (q[-1] - q[0]) if q[-1] > q[0] else np.zeros(101)
    measured, _ = measure(z, world.sea_fraction)
    def clean(v: np.ndarray) -> list[float | None]:
        return [float(x) if np.isfinite(x) else None for x in v]

    stats = {}
    for size, v in measured.items():
        names, real = REFERENCE[size]
        like = nearest(v, size)[1]
        stats[size] = {"map": clean(v), "like": like,
                       "real": clean(np.nanmean(real[[names.index(n) for n in like]], axis=0) if like
                                     else np.nanmean(real, axis=0))}
    return stats, q.round(5).tolist()


def _run(seed: int, trials: int, sea_fraction: float | None) -> None:
    """Build thread: build the map, publishing progress into ``STATE``."""
    def progress(world: Map, trial: int, total: int, best: float, accepted: bool, text: str) -> None:
        snapshot = _snapshot(world) if accepted else None
        relief = suggested_relief(world.height, world.sea_fraction) if accepted else None
        with LOCK:
            STATE.map, STATE.trial = world, trial
            # The objective is infinite until something is kept, and JSON has no infinity.
            STATE.history.append(best if np.isfinite(best) else None)
            STATE.message = f"{'Kept' if accepted else 'Dropped'}: {text}"
            if snapshot:
                STATE.accepted += 1
                STATE.stats, STATE.quantiles = snapshot
                STATE.sea_fraction, STATE.site = world.sea_fraction, world.site
                STATE.relief = None if relief is None else min(max(relief, 100.0), MAX_HEIGHT_M)

    try:
        build_map(seed, trials, sea_fraction, progress)
        with LOCK:
            STATE.message = f"Done: kept {STATE.accepted} of {trials} trials."
    except Exception:
        traceback.print_exc()
        with LOCK:
            STATE.error = traceback.format_exc(limit=1)
    finally:
        with LOCK:
            STATE.running = False


def _view(query: dict[str, list[str]]) -> View:
    """The ``View`` described by a request's query string (defaults for anything missing)."""
    def get(name: str, default: float) -> float:
        return float(query.get(name, [default])[0])

    return View(sea_fraction=min(max(get("sea", SEA_FRACTION * 100) / 100.0, 0.0), 0.99),
                vertical_m=min(max(get("scale", VERTICAL_M), 10.0), MAX_HEIGHT_M),
                sea_level_m=min(max(get("sl", EDITOR_SEA_LEVEL_M), 0.0), MAX_HEIGHT_M),
                cx=int(get("cx", GRID // 2)) % GRID, cy=int(get("cy", GRID // 2)) % GRID)


class Handler(BaseHTTPRequestHandler):
    """Serves the page and the JSON and PNG endpoints."""

    def _send(self, body: bytes, kind: str, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        """Send a complete response."""
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Send ``data`` as JSON."""
        self._send(json.dumps(data).encode(), "application/json", status)

    def do_GET(self) -> None:
        """Page, state, preview and heightmap downloads."""
        url = urlparse(self.path)
        query = parse_qs(url.query)
        with LOCK:
            state = json.dumps({k: v for k, v in STATE.__dict__.items() if k != "map"}).encode()
            world, seed, running = STATE.map, STATE.seed, STATE.running
        if url.path == "/":
            self._send(PAGE.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._send(state, "application/json")
        elif world is None or world.height.std() == 0:
            self._json({"error": "no map yet"}, HTTPStatus.NOT_FOUND)
        elif url.path not in ("/api/preview.png", "/api/heightmap/world.png", "/api/heightmap/playable.png"):
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        else:
            try:
                view = _view(query)
            except ValueError:
                self._json({"error": "sea, scale, sl, cx and cy must be numbers"}, HTTPStatus.BAD_REQUEST)
                return
            if url.path == "/api/preview.png":
                self._send(world.preview_png(view), "image/png")
                return
            if running:
                self._json({"error": "wait for the build to finish"}, HTTPStatus.CONFLICT)
                return
            kind = url.path.rsplit("/", 1)[1].removesuffix(".png")
            name = f"drunk_seed{seed}_sea{view.sea_fraction * 100:.0f}_x{view.cx}_y{view.cy}_{kind}.png"
            png = world.heightmap(view, kind, {"seed": seed, "gangs": len(world.gangs), "plains": len(world.plains)})
            self._send(png, "image/png", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def do_POST(self) -> None:
        """Start a build."""
        if urlparse(self.path).path != "/api/build":
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            seed, trials = int(body["seed"]) % 2**32, min(max(int(body.get("trials", TRIALS)), 1), 1000)
            sea = body.get("sea")
            sea_fraction = None if sea is None else min(max(float(sea), 0.0), 95.0) / 100.0
        except (ValueError, KeyError, TypeError):
            self._json({"error": "expected {\"seed\": int, \"trials\": int, \"sea\": percent or null}"},
                       HTTPStatus.BAD_REQUEST)
            return
        with LOCK:
            if STATE.running:
                self._json({"error": "a build is already running"}, HTTPStatus.CONFLICT)
                return
            STATE.__init__(seed=seed, running=True, trials=trials, message="Starting…",
                           sea_fraction=SEA_FRACTION if sea_fraction is None else sea_fraction)
        threading.Thread(target=_run, args=(seed, trials, sea_fraction), daemon=True).start()
        self._json({"ok": True})

    def log_message(self, format: str, *args: object) -> None:
        """Keep the console quiet: polling would flood it."""


def open_browser(url: str) -> None:
    """Open ``url`` in a browser; under WSL, in the Windows default browser (WSL forwards localhost)."""
    if "microsoft" in platform.release().lower():
        try:
            subprocess.run(["explorer.exe", url], check=False, timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    webbrowser.open(url)


def main() -> None:
    """Serve the UI until interrupted, opening it in the browser."""
    url = f"http://localhost:{PORT}/"
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise
        raise SystemExit(f"Port {PORT} is already in use: the app may already be running (open {url}), "
                         f"or another program is using the port.") from None
    print(f"Drunk terrain UI at {url} (Ctrl+C to stop)")
    open_browser(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


CONFIG = json.dumps({
    "grid": GRID, "playable": PLAYABLE_KM / WORLD_KM, "worldKm": WORLD_KM, "maxHeight": MAX_HEIGHT_M,
    "sea": SEA_FRACTION * 100, "scale": VERTICAL_M, "seaLevel": EDITOR_SEA_LEVEL_M, "trials": TRIALS,
    "statNames": STAT_NAMES, "sd": {k: np.nanstd(v, axis=0).tolist() for k, (_, v) in REFERENCE.items()},
})

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Drunk terrain</title>
<style>
  :root { --bg: #f3f4f6; --panel: #fff; --text: #1c2129; --muted: #5f6b78; --line: #dde1e6;
          --accent: #2563eb; --good: #15803d; --bad: #b91c1c; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #111418; --panel: #1b2027; --text: #e5e8ec; --muted: #97a3b0; --line: #2e353e;
            --accent: #6b9cff; --good: #4ade80; --bad: #f87171; }
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  body { margin: 0; background: var(--bg); color: var(--text); font: 14px/1.45 system-ui, sans-serif; }
  .app { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 16px; max-width: 1400px;
         margin: 0 auto; padding: 16px; }
  @media (max-width: 860px) { .app { grid-template-columns: 1fr; } }
  aside { display: flex; flex-direction: column; gap: 12px; }
  section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
  h1 { font-size: 18px; margin: 0; }
  h2 { font-size: 12px; margin: 0 0 10px; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
  .row { display: flex; gap: 8px; align-items: center; margin-top: 8px; }
  .row label { flex: 0 0 96px; color: var(--muted); }
  .row output { min-width: 64px; text-align: right; font-variant-numeric: tabular-nums; }
  input[type=number] { width: 100%; padding: 7px 9px; font: inherit; color: var(--text); background: var(--bg);
                       border: 1px solid var(--line); border-radius: 6px; }
  input[type=range] { flex: 1; accent-color: var(--accent); }
  input[type=checkbox] { margin: 0; accent-color: var(--accent); }
  input[type=range]:disabled { opacity: .45; }
  button { padding: 8px 14px; font: inherit; font-weight: 600; color: #fff; background: var(--accent);
           border: 0; border-radius: 6px; cursor: pointer; white-space: nowrap; }
  button.ghost { background: transparent; color: var(--accent); border: 1px solid var(--line); }
  button:disabled { opacity: .45; cursor: default; }
  progress { width: 100%; height: 8px; margin-top: 12px; accent-color: var(--accent); }
  .muted { color: var(--muted); font-size: 13px; }
  .warn { color: var(--bad); font-size: 13px; }
  #status { min-height: 2.9em; margin-top: 4px; }
  .heights { display: flex; justify-content: space-between; margin-top: 10px; font-variant-numeric: tabular-nums; }
  .heights div { text-align: center; } .heights b { display: block; font-size: 15px; }
  .exports { display: flex; gap: 8px; } .exports button { flex: 1; }
  main { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 12px; }
  .stage { position: relative; aspect-ratio: 1; border-radius: 6px; overflow: hidden; background: var(--bg);
           cursor: crosshair; }
  .stage img { position: absolute; inset: 0; width: 100%; height: 100%; image-rendering: auto; }
  .stage .empty { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }
  .square { position: absolute; border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,.6);
            pointer-events: none; }
  .square.hover { border-style: dashed; opacity: .8; }
  .caption { display: flex; justify-content: space-between; margin-top: 8px; }
  svg { width: 100%; height: 70px; display: block; }
  table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; margin-top: 8px; }
  th, td { padding: 3px 4px; text-align: right; } th:first-child, td:first-child { text-align: left; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  td.z { font-size: 12px; }
</style>
</head>
<body>
<div class="app">
  <aside>
    <section>
      <h1>Drunk terrain</h1>
      <div class="muted">Cities: Skylines II maps from gangs of random walkers.</div>
      <div class="row"><label for="seed">Seed</label><input id="seed" type="number" step="1" value="41">
        <button class="ghost" id="dice" title="Random seed">🎲</button></div>
      <div class="row"><label for="trials">Trials</label><input id="trials" type="number" min="1" max="1000"></div>
      <div class="row"><label for="autoSea">Auto sea</label><input id="autoSea" type="checkbox">
        <span class="muted">let the builder choose the sea share</span></div>
      <div class="row"><button id="build" style="flex:1">Build map</button></div>
      <progress id="progress" value="0" max="1"></progress>
      <div id="status" class="muted"></div>
    </section>
    <section>
      <h2>View</h2>
      <div class="row"><label for="sea">Sea</label><input id="sea" type="range" min="0" max="95" step="1">
        <output id="seaOut"></output></div>
      <div class="row"><label for="scale">Relief</label><input id="scale" type="range" min="100" step="10">
        <output id="scaleOut"></output></div>
      <div class="row"><label for="sl">Editor sea level</label><input id="sl" type="number" min="0" max="2000" step="0.1">
        <span class="muted">m</span></div>
      <div class="heights"><div><b id="low">–</b><span class="muted">lowest</span></div>
        <div><b id="coast">–</b><span class="muted">coast</span></div>
        <div><b id="high">–</b><span class="muted">highest</span></div></div>
      <div id="clip" class="warn"></div>
    </section>
    <section>
      <h2>Export heightmaps</h2>
      <div class="exports"><button id="world" disabled>World map</button><button id="playable" disabled>Playable area</button></div>
      <div class="muted" style="margin-top:8px">4096 × 4096 16-bit PNGs for the CS2 map editor, with the playable
        area where the square is.</div>
    </section>
    <section>
      <h2>Realism</h2>
      <svg id="chart" viewBox="0 0 300 70" preserveAspectRatio="none"></svg>
      <div class="muted" id="best">Objective: distance to the most similar real squares (0 = a match).</div>
      <table id="stats"></table>
    </section>
  </aside>
  <main>
    <div class="stage" id="stage">
      <div class="empty" id="empty">Build a map to see it here.</div>
      <img id="map" alt="" hidden>
      <div class="square" id="square" hidden></div>
      <div class="square hover" id="hover" hidden></div>
    </div>
    <div class="caption muted"><span>World map, __WORLD_KM__ km across. It wraps around at the edges.</span>
      <span>Click to move the playable area.</span></div>
  </main>
</div>
<script>
const C = __CONFIG__;
const $ = id => document.getElementById(id);
const view = { sea: C.sea, scale: C.scale, sl: C.seaLevel, cx: C.grid / 2, cy: C.grid / 2 };
let state = null, shownKey = "", loading = false, adopted = -1;

$("trials").value = C.trials; $("sea").value = view.sea; $("scale").max = C.maxHeight;
$("scale").value = view.scale; $("sl").value = view.sl;
const side = C.playable * 100;
Object.assign($("square").style, { left: (50 - side / 2) + "%", top: (50 - side / 2) + "%", width: side + "%", height: side + "%" });
Object.assign($("hover").style, { width: side + "%", height: side + "%" });

const query = () => `sea=${view.sea}&scale=${view.scale}&sl=${view.sl}&cx=${view.cx}&cy=${view.cy}`;
const m = x => `${Math.round(x).toLocaleString()} m`;

function heights() {
  $("seaOut").textContent = view.sea + "%";
  $("scaleOut").textContent = m(view.scale);
  if (!state || !state.quantiles.length) return;
  const h = state.quantiles[view.sea], low = view.sl - h * view.scale, high = view.sl + (1 - h) * view.scale;
  $("low").textContent = m(low); $("coast").textContent = m(view.sl); $("high").textContent = m(high);
  const notes = [];
  if (low < 0) notes.push("Sea floor below 0 m is flattened at 0 m.");
  if (high > C.maxHeight) notes.push(`Peaks above ${m(C.maxHeight)} are flattened.`);
  $("clip").textContent = notes.join(" ");
}

function redraw() {
  // Load the picture off-screen and swap it in, one request at a time, so dragging a slider doesn't flicker.
  if (!state || !state.accepted) return;
  const key = query() + `&seed=${state.seed}&v=${state.accepted}`;
  if (key === shownKey || loading) return;
  loading = true;
  const img = new Image();
  img.onload = () => { $("map").src = img.src; $("map").hidden = $("square").hidden = false; $("empty").hidden = true; done(); };
  img.onerror = done;
  img.src = "/api/preview.png?" + key;
  function done() { shownKey = key; loading = false; redraw(); }
}

function chart(history) {
  // Trials before anything was kept have no objective (null).
  const kept = history.map((v, i) => [i, v]).filter(([, v]) => v !== null);
  if (!kept.length) { $("chart").innerHTML = ""; $("best").textContent = "Objective: nothing kept yet."; return; }
  const top = Math.max(...kept.map(([, v]) => v), 1), n = Math.max(state.trials, 2);
  const pts = kept.map(([i, v]) => `${(i / (n - 1)) * 300},${70 - Math.min(v / top, 1) * 64 - 3}`).join(" ");
  $("chart").innerHTML = `<line x1="0" x2="300" y1="67" y2="67" stroke="var(--line)"/>` +
    `<polyline points="${pts}" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
  $("best").textContent = `Objective ${kept[kept.length - 1][1].toFixed(3)} after ${history.length} of ${state.trials} trials ` +
    `(${state.accepted} kept). 0 = matches its nearest real squares.`;
}

function table(stats) {
  if (!stats.world) { $("stats").innerHTML = ""; return; }
  const cell = (size, i) => {
    const v = stats[size].map[i], real = stats[size].real[i], sd = C.sd[size][i];
    const mean = real === null ? "–" : real.toFixed(2);
    if (v === null || real === null) return `<td>${v === null ? "–" : v.toFixed(2)}</td><td class="muted">${mean}</td><td></td>`;
    const z = (v - real) / sd;
    const colour = Math.abs(z) < 1 ? "var(--good)" : "var(--bad)";
    return `<td>${v.toFixed(2)}</td><td class="muted">${mean}</td><td class="z" style="color:${colour}">${z >= 0 ? "+" : ""}${z.toFixed(1)}σ</td>`;
  };
  $("stats").innerHTML = `<tr><th></th><th colspan="3">best city site</th><th colspan="3">world</th></tr>` +
    `<tr><th></th><th>map</th><th>real</th><th>z</th><th>map</th><th>real</th><th>z</th></tr>` +
    C.statNames.map((name, i) => `<tr><td>${name}</td>${cell("city", i)}${cell("world", i)}</tr>`).join("") +
    `<tr><td class="muted">like</td><td colspan="3" class="muted">${stats.city.like.join(", ")}</td>` +
    `<td colspan="3" class="muted">${stats.world.like.join(", ")}</td></tr>`;
}

async function poll() {
  try {
    state = await (await fetch("/api/state")).json();
    $("progress").value = state.trials ? state.trial / state.trials : 0;
    $("status").textContent = state.error || state.message;
    $("build").disabled = state.running;
    $("world").disabled = $("playable").disabled = state.running || !state.accepted;
    // While building, the sea share is the build's (fixed, or the builder's choice with auto sea), and the view
    // follows the city site and the relief of the most similar real places; afterwards they are yours to change.
    $("sea").disabled = state.running;
    if (state.running && state.accepted !== adopted) {
      adopted = state.accepted;
      view.sea = Math.round(state.sea_fraction * 100); $("sea").value = view.sea;
      [view.cx, view.cy] = state.site;
      if (state.relief) { view.scale = Math.round(state.relief / 10) * 10; $("scale").value = view.scale; }
    }
    chart(state.history); table(state.stats); heights(); redraw();
  } finally {
    setTimeout(poll, state && state.running ? 500 : 2000);
  }
}

$("build").onclick = async () => {
  const res = await fetch("/api/build", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ seed: +$("seed").value, trials: +$("trials").value,
                           sea: $("autoSea").checked ? null : view.sea }) });
  if (!res.ok) $("status").textContent = (await res.json()).error;
  adopted = -1;
  $("build").disabled = true;
};
$("dice").onclick = () => { $("seed").value = Math.floor(Math.random() * 100000); };
for (const id of ["sea", "scale", "sl"]) $(id).oninput = () => { view[id] = +$(id).value; heights(); redraw(); };
for (const kind of ["world", "playable"]) $(kind).onclick = () => { location.href = `/api/heightmap/${kind}.png?` + query(); };

const stage = $("stage");
const fraction = e => { const r = stage.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]; };
stage.onmousemove = e => {
  if ($("map").hidden) return;
  const [fx, fy] = fraction(e);
  Object.assign($("hover").style, { left: (fx * 100 - side / 2) + "%", top: (fy * 100 - side / 2) + "%" });
  $("hover").hidden = false;
};
stage.onmouseleave = () => { $("hover").hidden = true; };
stage.onclick = e => {
  if ($("map").hidden) return;
  const [fx, fy] = fraction(e), wrap = v => ((Math.round(v) % C.grid) + C.grid) % C.grid;
  view.cx = wrap(view.cx + (fx - 0.5) * C.grid); view.cy = wrap(view.cy + (fy - 0.5) * C.grid);
  redraw();
};
heights(); poll();
</script>
</body>
</html>
""".replace("__CONFIG__", CONFIG).replace("__WORLD_KM__", f"{WORLD_KM:g}")


if __name__ == "__main__":
    main()

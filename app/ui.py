"""Web UI: enter a seed, generate a layered-drunk terrain map, and view it in the browser.

Serves a single page at ``http://<ui.host>:<ui.port>/`` (``localhost:9000`` by
default) with a seed field, a Generate button, a progress bar and the map,
drawn exactly as ``main`` draws it. Generation runs in a background thread
through ``app.pipeline.generate_terrain``; the page polls for progress and
shows the PNG when it is ready. One map is generated at a time.

Uses only the Python standard library's HTTP server, so it needs no extra
dependencies. Endpoints:

- ``GET /``: the page.
- ``POST /api/generate`` with JSON ``{"seed": <int>}``: start a job; returns
  ``{"job": <id>}`` (409 if a job is already running).
- ``GET /api/progress/<id>``: ``{"fraction", "message", "done", "error"}``.
- ``GET /api/image/<id>``: the finished map as PNG.
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
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

# When run as a script (python app/ui.py) rather than a module (python -m app.ui),
# put the project root on sys.path so the `app` package can be imported.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.pipeline import generate_terrain
from app.rendering import save_terrain_map

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
  main { max-width: 760px; margin: 0 auto; padding: 24px 16px; }
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
  #map { display: block; width: 100%; height: auto; margin-top: 16px; border: 1px solid var(--border);
         border-radius: 6px; background: var(--panel); }
  #map[hidden] { display: none; }
</style>
</head>
<body>
<main>
  <h1>Drunk terrain</h1>
  <form class="controls" id="form">
    <label for="seed">Seed</label>
    <input id="seed" type="number" step="1" value="42" required>
    <button id="go" type="submit">Generate</button>
  </form>
  <progress id="bar" max="1" value="0"></progress>
  <div id="status">Enter a seed and press Generate.</div>
  <img id="map" alt="Generated terrain map" hidden>
</main>
<script>
const form = document.getElementById("form"), seedInput = document.getElementById("seed");
const go = document.getElementById("go"), bar = document.getElementById("bar");
const statusLine = document.getElementById("status"), map = document.getElementById("map");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const seed = parseInt(seedInput.value, 10);
  if (Number.isNaN(seed)) { statusLine.textContent = "The seed must be a whole number."; return; }
  go.disabled = true; bar.value = 0; statusLine.textContent = "Starting…";
  try {
    const response = await fetch("/api/generate", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({seed}),
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || response.statusText);
    poll(body.job, seed);
  } catch (error) {
    statusLine.textContent = "Could not start: " + error.message; go.disabled = false;
  }
});

async function poll(job, seed) {
  try {
    const response = await fetch(`/api/progress/${job}`);
    const state = await response.json();
    bar.value = state.fraction;
    statusLine.textContent = state.message;
    if (state.error) { statusLine.textContent = "Failed: " + state.error; go.disabled = false; return; }
    if (state.done) {
      map.src = `/api/image/${job}`; map.hidden = false;
      map.alt = `Generated terrain map for seed ${seed}`;
      go.disabled = false; return;
    }
  } catch (error) {
    statusLine.textContent = "Lost contact with the server: " + error.message; go.disabled = false; return;
  }
  setTimeout(() => poll(job, seed), 300);
}
</script>
</body>
</html>
"""


@dataclass
class Job:
    """One generation request and its progress; updated by the worker thread, read by request handlers."""

    seed: int
    fraction: float = 0.0
    message: str = "queued"
    done: bool = False
    error: str | None = None
    png: bytes | None = None
    lock: threading.Lock = dataclass_field(default_factory=threading.Lock)

    def update(self, fraction: float, message: str) -> None:
        """Record progress (called from the worker thread)."""
        with self.lock:
            self.fraction = min(max(fraction, 0.0), 1.0)
            self.message = message

    def snapshot(self) -> dict[str, object]:
        """A consistent copy of the job's state for the progress endpoint."""
        with self.lock:
            return {"fraction": self.fraction, "message": self.message, "done": self.done, "error": self.error}


class JobManager:
    """Runs one generation job at a time in a background thread and keeps finished jobs for viewing."""

    # Finished jobs kept for their images (older ones are dropped).
    KEEP = 10

    def __init__(self, config: Config) -> None:
        """Manage jobs generated with ``config``."""
        self.config = config
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._running: Job | None = None

    def start(self, seed: int) -> int | None:
        """Start generating ``seed``; returns the job id, or None if a job is already running."""
        with self._lock:
            if self._running is not None and not self._running.done:
                return None
            job_id = next(self._ids)
            job = Job(seed)
            self.jobs[job_id] = job
            self._running = job
            for old in sorted(self.jobs)[: -self.KEEP]:
                del self.jobs[old]
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job_id

    def _run(self, job: Job) -> None:
        """Worker thread: generate the terrain, render it to PNG bytes, and mark the job done."""
        try:
            result = generate_terrain(self.config, job.seed, progress=lambda f, m: job.update(0.97 * f, m))
            job.update(0.97, "drawing the map")
            buffer = io.BytesIO()
            save_terrain_map(buffer, result.field, result.grid, result.grid, result.title, result.sea_level,
                             rivers=result.river_area)
            summary = f"Seed {job.seed}: sea level {result.sea_level:.3f}"
            if result.state == "rivers":
                summary += f", {result.rivers_carved} rivers ({result.rivers_to_sea} reach the sea)"
            with job.lock:
                job.png = buffer.getvalue()
                job.fraction, job.message, job.done = 1.0, summary, True
        except Exception as exc:  # report any failure to the page rather than killing the thread silently
            traceback.print_exc()
            with job.lock:
                job.error, job.done = f"{type(exc).__name__}: {exc}", True


def make_handler(manager: JobManager) -> type[BaseHTTPRequestHandler]:
    """Build a request handler class bound to ``manager``."""

    class Handler(BaseHTTPRequestHandler):
        """Serves the page and the JSON/PNG API."""

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            """Send a complete response."""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
            """Send ``payload`` as JSON."""
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _job(self, prefix: str) -> Job | None:
            """The job named by the path after ``prefix``, or None (and a 404 sent)."""
            try:
                job = manager.jobs.get(int(self.path[len(prefix):]))
            except ValueError:
                job = None
            if job is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": "no such job"})
            return job

        def do_GET(self) -> None:
            """Serve the page, job progress, or a finished image."""
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.encode(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/progress/"):
                job = self._job("/api/progress/")
                if job is not None:
                    self._json(HTTPStatus.OK, job.snapshot())
            elif self.path.startswith("/api/image/"):
                job = self._job("/api/image/")
                if job is not None:
                    if job.png is None:
                        self._json(HTTPStatus.CONFLICT, {"error": "not finished"})
                    else:
                        self._send(HTTPStatus.OK, job.png, "image/png")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            """Start a generation job from ``{"seed": <int>}``."""
            if self.path != "/api/generate":
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                seed = int(json.loads(self.rfile.read(length) or b"{}")["seed"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "expected JSON {\"seed\": <integer>}"})
                return
            if seed < 0:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "the seed must not be negative"})
                return
            job_id = manager.start(seed)
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

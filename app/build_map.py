"""Build a map greedily: propose random gangs of drunks and keep each one only if the map looks more like real terrain.

Realism is measured by four scale-free statistics of a height field
(``stats``): spectral slope beta, roughness exponent H, hypsometric integral
and skewness. They are measured on the whole world and on each of its 16
playable-sized windows, and compared with the same statistics of real terrain
at the same two sizes (``REFERENCE``). The objective is the RMS z-score of
the world's statistics and of the windows' mean statistics (0 = matches real
terrain on average).

Each trial draws a gang with random step length, number of drunks, homeward
bias and "affinity" (how strongly its homes favour existing high ground),
renders it once, and tries it at a few weights relative to the map. The best
weight is kept if it lowers the objective; otherwise the gang is dropped.

Run as a script to build maps and save their previews:
``python -m app.build_map 41 42 43``.
"""

import math
import sys
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np

from app.drunk import Gang
from app.map import PLAYABLE_KM
from app.map import WORLD_KM
from app.map import Map
from app.map import View

# Real-terrain statistics (beta, H, HI, skew): mean and sd over square crops of six 1 x 1 degree Copernicus
# GLO-30 tiles (Appalachians, Rockies, Pyrenees, Alps, Hesse uplands, Scottish Highlands), measured with these
# functions: 96 crops 14.336 km across at 256 px, and 24 crops 57.344 km across at 1024 px.
REFERENCE = {
    "playable": (np.array([3.984, 0.567, 0.442, 0.095]), np.array([0.497, 0.140, 0.091, 0.544])),
    "world": (np.array([2.955, 0.348, 0.385, 0.287]), np.array([0.541, 0.087, 0.057, 0.435])),
}
STAT_NAMES = ["beta", "H", "HI", "skew"]
# Spectral slope is fitted over these wavenumbers (cycles per side), roughness over these lags (fractions of the side).
SPECTRUM_BAND = (4.0, 64.0)
LAG_BAND = (1.0 / 64.0, 1.0 / 4.0)
# Gangs tried per map.
TRIALS = 60
# Ranges the random gangs are drawn from (log-uniformly, except affinity).
STEP_KM = (0.075, 2.4)
DRUNKS_PER_STEP_AREA = (0.003, 0.1)  # drunks = this x (world side / step length)^2
MAX_DRUNKS = 4000
KAPPA_MAX = (0.01, 1.0)  # a gang's strongest bias; its drunks spread down to a tenth of it
AFFINITIES = (0.0, 1.0, 2.0, 4.0)  # homes are drawn with probability ~ normalised height ** affinity
# Weights tried for a gang, as its standard deviation relative to the map's.
RELATIVE_WEIGHTS = (0.125, 0.25, 0.5, 1.0, 2.0)


def stats(z: np.ndarray) -> np.ndarray:
    """``(beta, H, HI, skew)`` of each square field in the batch ``z`` (shape ``(b, n, n)``); returns ``(b, 4)``.

    beta is the slope of the radially averaged power spectrum (``P ~ k^-beta``)
    of the detrended, Hann-windowed field; H the slope of RMS height
    difference against lag; HI the mean height as a fraction of the range;
    skew the skewness of the heights.
    """
    b, n, _ = z.shape
    t = np.arange(n) - (n - 1) / 2.0
    d = z - z.mean(axis=(1, 2), keepdims=True)
    d = d - (d * t).sum(axis=(1, 2))[:, None, None] * t / (n * t @ t) \
          - (d * t[:, None]).sum(axis=(1, 2))[:, None, None] * t[:, None] / (n * t @ t)
    power = np.abs(np.fft.fft2(d * np.outer(np.hanning(n), np.hanning(n)))) ** 2
    k = np.fft.fftfreq(n) * n
    k = np.hypot(k[:, None], k[None, :]).ravel()
    edges = np.geomspace(*SPECTRUM_BAND, 17)
    band = (k >= edges[0]) & (k < edges[-1])
    bins = np.digitize(k[band], edges) - 1
    counts = np.bincount(bins, minlength=16)
    means = np.stack([np.bincount(bins, p.ravel()[band], minlength=16) / counts for p in power])
    beta = -np.polyfit(np.log(np.sqrt(edges[:-1] * edges[1:])), np.log(means.T), 1)[0]

    lags = np.unique(np.geomspace(max(1, LAG_BAND[0] * n), LAG_BAND[1] * n, 12).astype(int))
    rms = np.stack([np.sqrt((np.mean((z[:, :, s:] - z[:, :, :-s]) ** 2, axis=(1, 2))
                             + np.mean((z[:, s:] - z[:, :-s]) ** 2, axis=(1, 2))) / 2.0) for s in lags])
    h = np.polyfit(np.log(lags), np.log(rms), 1)[0]

    lo, hi = z.min(axis=(1, 2)), z.max(axis=(1, 2))
    hypsometric = (z.mean(axis=(1, 2)) - lo) / (hi - lo)
    c = z - z.mean(axis=(1, 2), keepdims=True)
    skew = (c**3).mean(axis=(1, 2)) / c.std(axis=(1, 2)) ** 3
    return np.column_stack([beta, h, hypsometric, skew])


def measure(height: np.ndarray) -> dict[str, np.ndarray]:
    """The statistics of the whole world and the mean over its playable-sized windows."""
    k = round(WORLD_KM / PLAYABLE_KM)
    w = height.shape[0] // k
    windows = height[: k * w, : k * w].reshape(k, w, k, w).swapaxes(1, 2).reshape(k * k, w, w)
    return {"world": stats(height[None])[0], "playable": stats(windows).mean(axis=0)}


def objective(height: np.ndarray) -> float:
    """RMS z-score of the map's statistics against real terrain, at world and playable size (lower is better)."""
    if height.std() == 0:
        return math.inf
    z = [(value - REFERENCE[size][0]) / REFERENCE[size][1] for size, value in measure(height).items()]
    return float(np.sqrt(np.mean(np.square(z))))


def random_gang(rng: np.random.Generator, height: np.ndarray) -> tuple[Gang, str]:
    """A gang with random settings (see the ranges above), and a short description of it."""
    def log_uniform(lo: float, hi: float) -> float:
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))

    step = log_uniform(*STEP_KM)
    count = int(np.clip(log_uniform(*DRUNKS_PER_STEP_AREA) * (WORLD_KM / step) ** 2, 10, MAX_DRUNKS))
    kappa = log_uniform(*KAPPA_MAX)
    affinity = float(rng.choice(AFFINITIES))
    n = height.shape[0]
    p = ((height - height.min()) / np.ptp(height)) ** affinity if np.ptp(height) > 0 else np.ones_like(height)
    cells = rng.choice(n * n, size=count, p=(p / p.sum()).ravel())
    homes = (np.column_stack([cells % n, cells // n]) + rng.random((count, 2))) * WORLD_KM / n
    gang = Gang(rng.integers(0, 2**32, count), homes, kappa * 10 ** rng.uniform(-1.0, 0.0, count), step)
    return gang, f"{count} drunks, {step * 1000:.0f} m steps, bias {kappa:.2f}, affinity {affinity:g}"


def build_map(seed: int, trials: int = TRIALS,
              progress: Callable[[Map, int, int, float, bool, str], None] | None = None) -> Map:
    """Build a map from ``seed`` by greedily adding the random gangs that bring it closer to real terrain.

    ``progress``, if given, is called after each trial with ``(map so far,
    trial, trials, best objective, accepted, description)``.
    """
    rng = np.random.default_rng(seed)
    world = Map()
    best = math.inf
    for trial in range(1, trials + 1):
        gang, description = random_gang(rng, world.height)
        field = gang.field(WORLD_KM, world.grid)
        scale = world.height.std() / field.std()
        weights = [1.0 / field.std()] if scale == 0 else [r * scale for r in RELATIVE_WEIGHTS]
        score, weight = min((objective(world.height + w * field), w) for w in weights)
        accepted = score < best
        if accepted:
            world.add(gang, weight, field)
            best = score
        if progress is not None:
            progress(world, trial, trials, best, accepted, description)
    return world


def main() -> None:
    """Build a map for each seed on the command line (default 41) and save its preview to ``output/``."""
    out = Path(__file__).resolve().parent.parent / "output"
    out.mkdir(exist_ok=True)
    for seed in [int(s) for s in sys.argv[1:]] or [41]:
        started = time.time()
        world = build_map(seed, progress=lambda _, t, n, best, ok, text: print(
            f"  {t:3d}/{n} {'+' if ok else ' '} {best:.3f}  {text}", flush=True))
        measured = measure(world.height)
        print(f"seed {seed}: {len(world.gangs)} gangs, objective {objective(world.height):.3f}, "
              f"{time.time() - started:.0f} s")
        for size, (mean, sd) in REFERENCE.items():
            print(f"  {size:9s}" + "".join(f"  {name} {v:.2f} (real {m:.2f} ± {s:.2f})"
                                          for name, v, m, s in zip(STAT_NAMES, measured[size], mean, sd)))
        (out / f"map_seed{seed}.png").write_bytes(world.preview_png(View()))


if __name__ == "__main__":
    main()

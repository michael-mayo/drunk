"""Build a map greedily: propose random gangs of drunks and keep each one only if the map looks more like real terrain.

Realism is measured by nine scale-free statistics of a height field with its
water masked out (``stats``): the roughness exponent H at fine and at coarse
lags, the hypsometric integral and skewness of the land, the water share,
three of its drainage (the share of land in closed hollows, the concavity of
channel profiles, and the exponent tau of the distribution of drained areas),
and the contrast between steep high ground and flat low ground.
They are compared with real FABDEM terrain (``REFERENCE``, every site's
statistics, made by ``fabdem/reference.py``) in two ways:

- the whole world against 61 real 57 km squares, 31 centred on cities and
  30 on wild terrain;
- the most city-like of the world's 16 playable-sized windows against the
  14 km squares around the 31 cities, so every map has a real-looking city site.

Each is scored by its distance to the few most similar real squares
(``nearest``), so a map should look like some real place rather than an
average of very different ones; the objective is the RMS of the two
distances (0 = matches real squares exactly).
The sea share is set by the user, or, in "auto" mode, chosen again
whenever a gang is kept as the one that best fits real terrain.

Each trial draws either a gang, with random step length, number of drunks,
bias and "affinity" (how strongly its homes favour existing high ground), or,
once the map has some relief, an alluvial plain (``app.plains``) with random
settings. A gang is rendered once and tried at a few weights relative to the
map, and the best weight is kept if it lowers the objective; plains are kept if
they lower it. Anything else is dropped.

Run as a script to build maps and save their previews:
``python -m app.build_map 41 42 43 [--sea 30]``.
"""

import argparse
import heapq
import math
import time
from collections.abc import Callable
from pathlib import Path

import numba
import numpy as np

from app.drunk import Gang
from app.map import PLAYABLE_KM
from app.map import WORLD_KM
from app.map import Map
from app.map import View
from app.plains import Plains

# Real terrain: every FABDEM reference square's statistics (see STAT_NAMES), written by fabdem/reference.py.
# "world": 57.344 km squares at 1024 px around 31 cities and 30 wild sites; "city": the central 14.336 km
# (256 px) of each city square. Each entry is (site names, statistics with one row per site).
# RELIEF has each site's land relief in metres (1st to 99th percentile), in the same order.
# (Both are empty only while fabdem/reference.py makes them.)
_SAVED = Path(__file__).resolve().parent.parent / "fabdem" / "data" / "reference.npz"
_LOADED = [np.load(_SAVED)] if _SAVED.exists() else []
REFERENCE = {size: (list(saved[f"{size}_names"]), saved[size]) for saved in _LOADED for size in ("world", "city")}
RELIEF = {size: saved[f"{size}_relief"] for saved in _LOADED for size in ("world", "city") if f"{size}_relief" in saved}
# A map is scored by its distance to this many of the most similar real squares.
NEIGHBOURS = 3
STAT_NAMES = ["H fine", "H coarse", "HI", "skew", "water", "hollows", "concavity", "tau", "contrast"]
# Roughness is fitted over these lags, as fractions of the side: fine and coarse.
FINE_LAGS = (1.0 / 64.0, 1.0 / 16.0)
COARSE_LAGS = (1.0 / 16.0, 1.0 / 4.0)
# Land filled by more than this share of the relief to drain counts as a closed hollow.
HOLLOW_DEPTH = 1e-3
# Channels: cells draining at least this many cells, and steeper than this share of the relief per cell
# (filled hollows are nearly flat and would swamp the slope-area fit).
CHANNEL_MIN_AREA = 50
CHANNEL_MIN_SLOPE = 1e-4
# Contrast compares slopes on low land (up to this share of the relief, or the lowest 1% of land if that is more)
# with slopes on high land (from this share up).
CONTRAST_LOW = 0.05
CONTRAST_HIGH = 0.4
# Fields with less land than this have no statistics.
MIN_LAND = 0.05
# Sea shares tried whenever a gang or plains are kept.
SEA_FRACTIONS = np.arange(0.0, 0.61, 0.05)
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
# Chance that a trial (after the first gang is kept) proposes plains instead of a gang.
PLAINS_CHANCE = 0.25
# Ranges plains are drawn from (log-uniformly, except PLAIN_SHARE, uniform): the mountain front as a share of the
# land's relief, the plain's top as a share of the front, rise length, share of small relief kept, smoothing.
FRONT = (0.08, 0.4)
PLAIN_SHARE = (0.05, 0.5)
LENGTH_KM = (1.5, 20.0)
KEEP = (0.02, 0.3)
SMOOTH_KM = (0.4, 2.5)


@numba.njit(cache=True)
def _drain(z: np.ndarray, land: np.ndarray, epsilon: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Priority-flood drainage: ``(filled heights, downstream cell, drained area)``, cells numbered row by row.

    Water and edge cells are outlets (water before anything else, whatever
    its height, so heights under water don't matter). Land is reached from the outlets in
    order of rising (filled) height; each cell drains into the neighbour it
    was reached from, and a cell lower than that neighbour (in a hollow) is
    raised to just above it. Drained area counts the land cells upstream,
    the cell included.
    """
    ny, nx = z.shape
    filled = z.ravel().copy()
    downstream = np.full(ny * nx, -1)
    seen = np.zeros(ny * nx, dtype=np.bool_)
    heap = [(0.0, 0)]
    heap.pop()
    for c in range(ny * nx):
        i, j = c // nx, c % nx
        if not land[i, j]:
            heap.append((-np.inf, c))
            seen[c] = True
        elif i == 0 or j == 0 or i == ny - 1 or j == nx - 1:
            heap.append((filled[c], c))
            seen[c] = True
    heapq.heapify(heap)
    order = np.empty(ny * nx, dtype=np.int64)
    count = 0
    while heap:
        h, c = heapq.heappop(heap)
        order[count] = c
        count += 1
        i, j = c // nx, c % nx
        for di in range(-1, 2):
            for dj in range(-1, 2):
                a, b = i + di, j + dj
                if 0 <= a < ny and 0 <= b < nx and not seen[a * nx + b]:
                    d = a * nx + b
                    seen[d] = True
                    downstream[d] = c
                    filled[d] = max(filled[d], h + epsilon)
                    heapq.heappush(heap, (filled[d], d))
    area = land.ravel().astype(np.float64)
    for k in range(count - 1, -1, -1):
        c = order[k]
        if downstream[c] >= 0:
            area[downstream[c]] += area[c]
    return filled.reshape(ny, nx), downstream, area


def _fit(x: np.ndarray, y: np.ndarray) -> float:
    """Slope of the straight line through ``(log x, log y)`` (NaN unless every y is positive)."""
    y = np.asarray(y, dtype=float)
    return float(np.polyfit(np.log(x), np.log(y), 1)[0]) if np.all(y > 0) else np.nan


def _land_slope(z: np.ndarray, land: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Slope (height per cell) from differences between neighbouring land cells only, and where it is defined.

    Each axis's gradient is the mean of the differences to the cell's land
    neighbours along it, so a coastline's drop into the sea (or a real
    square's flat water) never counts as slope.
    """
    gradients = []
    defined = land.copy()
    for axis in (0, 1):
        ahead = [slice(None), slice(None)]
        behind = [slice(None), slice(None)]
        ahead[axis], behind[axis] = slice(1, None), slice(None, -1)
        ahead, behind = tuple(ahead), tuple(behind)
        valid = land[ahead] & land[behind]
        step = np.where(valid, z[ahead] - z[behind], 0.0)
        total, count = np.zeros(z.shape), np.zeros(z.shape)
        for side in (ahead, behind):
            total[side] += step
            count[side] += valid
        gradients.append(total / np.maximum(count, 1))
        defined &= count > 0
    return np.hypot(*gradients), defined


def stats(z: np.ndarray, land: np.ndarray) -> np.ndarray:
    """The ``STAT_NAMES`` statistics of square field ``z`` where ``land`` is True (NaN if too little land).

    H is the slope of RMS height difference against lag (log-log), over
    pairs of land cells along both axes. HI is the mean land height as a
    fraction of its 1st-99th percentile range (the relief), and skew the
    skewness of land heights. Water is the share of cells that are not land.

    Drainage (``_drain``, with water and the edges as outlets): hollows is
    the share of land raised by more than ``HOLLOW_DEPTH`` of the relief to
    drain; concavity is theta in channel slope ~ drained area^-theta (median
    slope in logarithmic area bins); tau is the exponent in
    P(drained area >= a) ~ a^-tau, for a from 4 cells to 1/256 of the field
    (up to the largest area there is).

    Contrast is log10 of the median slope on high land over that on low land
    (see ``CONTRAST_LOW`` and ``CONTRAST_HIGH``): real coasts and basins are
    flat at the bottom and steep higher up.
    """
    n = z.shape[0]
    if land.mean() < MIN_LAND:
        return np.full(len(STAT_NAMES), np.nan)

    def roughness(band: tuple[float, float]) -> float:
        lags = np.unique(np.geomspace(max(1, band[0] * n), band[1] * n, 6).astype(int))
        rms = []
        for s in lags:
            pairs = [(z[:, s:] - z[:, :-s], land[:, s:] & land[:, :-s]), (z[s:] - z[:-s], land[s:] & land[:-s])]
            count = sum(m.sum() for _, m in pairs)
            if count == 0:
                return np.nan
            rms.append(math.sqrt(sum(np.where(m, d * d, 0.0).sum() for d, m in pairs) / count))
        return _fit(lags, rms)

    h = z[land]
    lo, hi = np.percentile(h, [1, 99])
    relief = hi - lo
    if relief <= 0:
        return np.full(len(STAT_NAMES), np.nan)
    c = h - h.mean()

    filled, downstream, area = _drain(np.ascontiguousarray(z, dtype=np.float64), np.ascontiguousarray(land),
                                      1e-9 * relief)
    hollows = float(np.mean((filled - z)[land] > HOLLOW_DEPTH * relief))
    # Channel steps between two land cells (the last step, into water, has no meaningful slope).
    cells = np.flatnonzero(land.ravel() & (downstream >= 0))
    cells = cells[land.ravel()[downstream[cells]]]
    below = downstream[cells]
    diagonal = (cells % n != below % n) & (cells // n != below // n)
    slope = (filled.ravel()[cells] - filled.ravel()[below]) / np.where(diagonal, math.sqrt(2.0), 1.0)
    a = area[cells]
    channel = (a >= CHANNEL_MIN_AREA) & (slope > CHANNEL_MIN_SLOPE * relief)
    edges = np.geomspace(CHANNEL_MIN_AREA, max(a.max(), 2 * CHANNEL_MIN_AREA), 13)
    bins = [(lo_, hi_) for lo_, hi_ in zip(edges[:-1], edges[1:])
            if np.sum(channel & (a >= lo_) & (a < hi_)) >= 10]
    concavity = -_fit([math.sqrt(l * u) for l, u in bins],
                      [np.median(slope[channel & (a >= l) & (a < u)]) for l, u in bins]) if len(bins) >= 3 else np.nan
    sizes = np.geomspace(4, n * n / 256, 8)
    drained = area[land.ravel()]
    sizes = sizes[sizes <= drained.max()]
    tau = -_fit(sizes, [np.mean(drained >= s) for s in sizes]) if len(sizes) >= 3 else np.nan

    slope, defined = _land_slope(z, land)
    t = (z - lo) / relief
    low_top = max(CONTRAST_LOW, np.quantile(t[defined], 0.01)) if defined.any() else CONTRAST_LOW
    low, high = slope[defined & (t <= low_top)], slope[defined & (t >= CONTRAST_HIGH)]
    # Floor the low slope: real squares' flattest land can measure exactly 0 at whole decimetres.
    contrast = (math.log10(np.median(high) / max(np.median(low), 1e-6 * relief))
                if len(low) and len(high) else np.nan)

    return np.array([roughness(FINE_LAGS), roughness(COARSE_LAGS), (h.mean() - lo) / relief,
                     (c**3).mean() / c.std() ** 3, 1.0 - land.mean(), hollows, concavity, tau, contrast])


def windows(a: np.ndarray) -> np.ndarray:
    """The 16 playable-sized windows tiling world-sized array ``a``, shape ``(16, w, w)``."""
    k = round(WORLD_KM / PLAYABLE_KM)
    w = a.shape[0] // k
    return a[: k * w, : k * w].reshape(k, w, k, w).swapaxes(1, 2).reshape(k * k, w, w)


def nearest(values: np.ndarray, size: str) -> tuple[float, list[str]]:
    """Distance from statistics ``values`` to the ``NEIGHBOURS`` most similar real squares of ``size``, and their names.

    The distance to a square is the RMS difference of the statistics, each in
    standard deviations of real terrain (so 0 = identical); the result is the mean over the nearest squares.
    Scoring against the nearest squares rather than the average of all of them
    asks for terrain like some real place, not a blend of the Alps and Kansas.
    """
    names, real = REFERENCE[size]
    if not np.isfinite(values).all():
        return math.inf, []
    distance = np.sqrt(np.nanmean(((real - values) / np.nanstd(real, axis=0)) ** 2, axis=1))
    order = np.argsort(distance)[:NEIGHBOURS]
    return float(distance[order].mean()), [names[i] for i in order]


def measure(height: np.ndarray, sea_fraction: float) -> tuple[dict[str, np.ndarray], int]:
    """The world's statistics and those of its most city-like window, and that window's index."""
    land = height > np.quantile(height, sea_fraction)
    candidates = [stats(z, m) for z, m in zip(windows(height), windows(land))]
    best = int(np.argmin([nearest(c, "city")[0] for c in candidates]))
    return {"world": stats(height, land), "city": candidates[best]}, best


def objective(height: np.ndarray, sea_fraction: float) -> float:
    """RMS of the world's and its best city site's distances to real terrain (lower is better; inf if undefined)."""
    if height.std() == 0:
        return math.inf
    measured, _ = measure(height, sea_fraction)
    return float(np.sqrt(np.mean([nearest(v, size)[0] ** 2 for size, v in measured.items()])))


def centre(index: int, n: int) -> tuple[int, int]:
    """Centre cell ``(cx, cy)`` of playable window ``index`` (as numbered by ``windows``) of an ``n`` x ``n`` world."""
    k = round(WORLD_KM / PLAYABLE_KM)
    w = n // k
    return (index % k) * w + w // 2, (index // k) * w + w // 2


def site(height: np.ndarray, sea_fraction: float) -> tuple[int, int]:
    """Centre cell ``(cx, cy)`` of the most city-like playable window."""
    return centre(measure(height, sea_fraction)[1], height.shape[0])


def suggested_relief(height: np.ndarray, sea_fraction: float) -> float | None:
    """Metres from lowest to highest point that give the map the land relief of its nearest real squares.

    The statistics are scale-free, so they can't tell the Pampas from the
    Alps by height alone; the median relief of the world's nearest real
    squares fills that in. None if the map or the reference can't say.
    """
    if "world" not in RELIEF or height.std() == 0:
        return None
    land = height > np.quantile(height, sea_fraction)
    _, like = nearest(stats(height, land), "world")
    if not like:
        return None
    names = REFERENCE["world"][0]
    real = float(np.median(RELIEF["world"][[names.index(name) for name in like]]))
    lo, hi = np.percentile(height[land], [1, 99])
    return real * float(np.ptp(height)) / (hi - lo)


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


def random_plains(rng: np.random.Generator) -> Plains:
    """Plains with random settings (see the ranges above)."""
    def log_uniform(lo: float, hi: float) -> float:
        return math.exp(rng.uniform(math.log(lo), math.log(hi)))

    front = log_uniform(*FRONT)
    return Plains(front=front, plain=front * rng.uniform(*PLAIN_SHARE), length_km=log_uniform(*LENGTH_KM),
                  keep=log_uniform(*KEEP), smooth_km=log_uniform(*SMOOTH_KM))


def build_map(seed: int, trials: int = TRIALS, sea_fraction: float | None = None,
              progress: Callable[[Map, int, int, float, bool, str], None] | None = None) -> Map:
    """Build a map from ``seed`` by greedily adding the random gangs and plains that bring it closer to real terrain.

    The map keeps ``sea_fraction`` of its area under the sea; if it is None
    ("auto"), the builder chooses the share that fits real terrain best,
    and may change it as gangs and plains are kept. ``progress``, if given, is called after each trial with
    ``(map so far, trial, trials, best objective, accepted, description)``.
    """
    rng = np.random.default_rng(seed)
    world = Map()
    if sea_fraction is not None:
        world.sea_fraction = sea_fraction
    best = math.inf
    for trial in range(1, trials + 1):
        if world.steps and rng.random() < PLAINS_CHANCE:
            plains = random_plains(rng)
            description = plains.describe()
            height = plains.apply(world.height, world.sea_fraction, WORLD_KM)
            score = objective(height, world.sea_fraction)
            accepted = score < best
            if accepted:
                world.lay(plains, height)
        else:
            gang, description = random_gang(rng, world.height)
            field = gang.field(WORLD_KM, world.grid)
            scale = world.height.std() / field.std()
            weights = [1.0 / field.std()] if scale == 0 else [r * scale for r in RELATIVE_WEIGHTS]
            score, weight = min((objective(world.height + w * field, world.sea_fraction), w) for w in weights)
            accepted = score < best
            if accepted:
                world.add(gang, weight, field)
        if accepted:
            best = score
            if sea_fraction is None:
                # Pick the sea share again: from all of them at first, then from the current one's neighbours.
                near = SEA_FRACTIONS if world.steps == 1 else \
                    SEA_FRACTIONS[np.abs(SEA_FRACTIONS - world.sea_fraction) < 0.051]
                best, world.sea_fraction = min((objective(world.height, f), float(f)) for f in near)
            world.site = site(world.height, world.sea_fraction)
        if progress is not None:
            progress(world, trial, trials, best, accepted, description)
    return world


def main() -> None:
    """Build a map for each seed on the command line and save its preview to ``output/``."""
    parser = argparse.ArgumentParser(description="Build maps and save their previews to output/.")
    parser.add_argument("seeds", nargs="*", type=int, default=[41])
    parser.add_argument("--sea", type=float, help="percentage of the map under the sea (default: auto)")
    args = parser.parse_args()
    out = Path(__file__).resolve().parent.parent / "output"
    out.mkdir(exist_ok=True)
    for seed in args.seeds:
        started = time.time()
        world = build_map(seed, sea_fraction=None if args.sea is None else args.sea / 100,
                          progress=lambda _, t, n, best, ok, text: print(
            f"  {t:3d}/{n} {'+' if ok else ' '} {best:.3f}  {text}", flush=True))
        measured, _ = measure(world.height, world.sea_fraction)
        relief = suggested_relief(world.height, world.sea_fraction)
        print(f"seed {seed}: {len(world.gangs)} gangs, {len(world.plains)} plains, {world.sea_fraction:.0%} sea, "
              f"objective {objective(world.height, world.sea_fraction):.3f}, {time.time() - started:.0f} s")
        for size, v in measured.items():
            distance, like = nearest(v, size)
            print(f"  {size:6s} {distance:.2f} from {', '.join(like)}:" +
                  "".join(f"  {name} {x:.2f}" for name, x in zip(STAT_NAMES, v)))
        print(f"  suggested relief: {relief:.0f} m" if relief else "  no suggested relief")
        view = View(sea_fraction=world.sea_fraction, cx=world.site[0], cy=world.site[1])
        (out / f"map_seed{seed}.png").write_bytes(world.preview_png(view))


if __name__ == "__main__":
    main()

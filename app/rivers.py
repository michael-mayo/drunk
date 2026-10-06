"""River drunks: walkers that follow the drainage downhill and carve graded river valleys.

Each river drunk starts at a source on land and walks one grid cell per step.
Its direction is drawn from a von Mises distribution centred on a blend of
its previous heading (``inertia``) and the smoothed downstream direction of
the drained terrain, with concentration ``kappa``, so rivers meander but
follow the drainage. It stops when it reaches the sea or meets a river
already carved, becoming a tributary. If it stops making progress towards
the sea (circling on flat ground), it switches to the steepest-descent path.

Each finished path is then carved. Its riverbed descends monotonically from
the source to the mouth (the sea, or the riverbed at the junction, so
tributaries join at the same level), with a graded profile:
``bed slope = k * A ** -concavity``, where ``A`` is the catchment area
draining through each point and ``k`` is set so the bed starts at the
source's height. This is the channel-concavity relation measured on real
rivers (``slope ~ area ** -theta``). Around the bed the terrain is lowered
towards it with a Gaussian cross-section whose width grows with catchment
area (``valley_width * sqrt(A)`` cells), forming the valley.

Rivers are walked in order of decreasing source height, so long trunk
rivers tend to be carved first and shorter ones join them. All work is on
the wrap-around map, in compiled numba code, from a fixed seed.
"""

import math
from dataclasses import dataclass

import numba
import numpy as np

from app.drainage import fill_hollows
from app.drainage import flow_accumulation
from app.sampling import poisson_disk_points

# The 8 neighbour offsets (dy, dx) and their distances in cells.
_DY = np.array([-1, -1, -1, 0, 0, 1, 1, 1], dtype=np.int64)
_DX = np.array([-1, 0, 1, -1, 1, -1, 0, 1], dtype=np.int64)
_DIST = np.sqrt(_DY.astype(np.float64) ** 2 + _DX.astype(np.float64) ** 2)


@dataclass(frozen=True)
class RiverParams:
    """Settings for river drunks; lengths in grid cells, heights in normalised (0-1) units."""

    # Number of river sources (Poisson-disk sampled over the map; those in the sea are dropped).
    sources: int = 100
    # Sources must be at least this far above sea level (fraction of the land's height range).
    min_source_height: float = 0.05
    # Gaussian smoothing (cells) of the downstream-direction field the drunks follow.
    direction_smoothing: float = 2.0
    # von Mises concentration around the downstream direction (lower = more meandering).
    kappa: float = 16.0
    # Share of the previous heading kept each step.
    inertia: float = 0.5
    # Channel concavity theta of the graded riverbed: bed slope ~ area ** -concavity.
    concavity: float = 0.45
    # Valley half-width (Gaussian sigma, cells) = valley_width * sqrt(catchment area in cells).
    valley_width: float = 0.03
    # Narrowest valley sigma, in cells.
    min_valley_sigma: float = 1.0
    # Steps without getting closer to the sea before switching to steepest descent.
    stall_steps: int = 50


@numba.njit(cache=True)
def _receivers_and_distance(h: np.ndarray, sea: np.ndarray, dy: np.ndarray, dx: np.ndarray, dist: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Steepest-descent receiver (dy, dx) of every cell of wrap-around ``h``, and flow distance to the sea.

    Sea cells, and cells with no lower neighbour, have no receiver (0, 0).
    Distances accumulate along receivers, processed in rising height order.
    """
    n_y, n_x = h.shape
    rdy = np.zeros((n_y, n_x), dtype=np.int64)
    rdx = np.zeros((n_y, n_x), dtype=np.int64)
    distance = np.zeros((n_y, n_x))
    for y in range(n_y):
        for x in range(n_x):
            if sea[y, x]:
                continue
            best = 0.0
            for k in range(8):
                s = (h[y, x] - h[(y + dy[k]) % n_y, (x + dx[k]) % n_x]) / dist[k]
                if s > best:
                    best = s
                    rdy[y, x] = dy[k]
                    rdx[y, x] = dx[k]
    order = np.argsort(h.ravel())
    for idx in order:
        y = idx // n_x
        x = idx % n_x
        if sea[y, x] or (rdy[y, x] == 0 and rdx[y, x] == 0):
            continue
        ry = (y + rdy[y, x]) % n_y
        rx = (x + rdx[y, x]) % n_x
        distance[y, x] = distance[ry, rx] + math.sqrt(rdy[y, x] ** 2 + rdx[y, x] ** 2)
    return rdy, rdx, distance


@numba.njit(cache=True)
def _bilinear(f: np.ndarray, x: float, y: float) -> float:
    """Bilinear sample of wrap-around grid ``f`` at ``(x, y)`` in cells."""
    n_y, n_x = f.shape
    ix = int(math.floor(x))
    iy = int(math.floor(y))
    fx = x - ix
    fy = y - iy
    x0 = ix % n_x
    y0 = iy % n_y
    x1 = (x0 + 1) % n_x
    y1 = (y0 + 1) % n_y
    return (f[y0, x0] * (1 - fx) * (1 - fy) + f[y0, x1] * fx * (1 - fy)
            + f[y1, x0] * (1 - fx) * fy + f[y1, x1] * fx * fy)


@numba.njit(cache=True)
def _walk(
    sx: int,
    sy: int,
    ux: np.ndarray,
    uy: np.ndarray,
    rdy: np.ndarray,
    rdx: np.ndarray,
    distance: np.ndarray,
    sea: np.ndarray,
    river: np.ndarray,
    kappa: float,
    inertia: float,
    stall_steps: int,
    max_steps: int,
) -> tuple[np.ndarray, np.ndarray, int]:
    """Walk one river drunk from cell ``(sx, sy)``; returns path cells (x, y) and how it ended.

    End codes: 1 = reached the sea, 2 = joined an existing river, 0 = gave up.
    Consecutive duplicate cells are dropped, so the path is a chain of cells.
    """
    n_y, n_x = sea.shape
    xs = [sx]
    ys = [sy]
    x = sx + 0.5
    y = sy + 0.5
    hx, hy = 0.0, 0.0
    best = distance[sy, sx]
    since_best = 0
    steepest = False
    for _ in range(max_steps):
        cx = xs[-1]
        cy = ys[-1]
        if steepest:
            ddy = rdy[cy, cx]
            ddx = rdx[cy, cx]
            if ddy == 0 and ddx == 0:
                return np.array(xs), np.array(ys), 0
            nx_ = (cx + ddx) % n_x
            ny_ = (cy + ddy) % n_y
            x = nx_ + 0.5
            y = ny_ + 0.5
        else:
            mx = inertia * hx + (1.0 - inertia) * _bilinear(ux, x - 0.5, y - 0.5)
            my = inertia * hy + (1.0 - inertia) * _bilinear(uy, x - 0.5, y - 0.5)
            if mx == 0.0 and my == 0.0:
                steepest = True
                continue
            angle = np.random.vonmises(math.atan2(my, mx), kappa)
            hx = math.cos(angle)
            hy = math.sin(angle)
            x += hx
            y += hy
            nx_ = int(math.floor(x)) % n_x
            ny_ = int(math.floor(y)) % n_y
        if nx_ == cx and ny_ == cy:
            continue
        # Moving diagonally between cells is fine; jumping further is not possible (step = 1 cell).
        xs.append(nx_)
        ys.append(ny_)
        if sea[ny_, nx_]:
            return np.array(xs), np.array(ys), 1
        if river[ny_, nx_]:
            return np.array(xs), np.array(ys), 2
        if distance[ny_, nx_] < best - 1e-9:
            best = distance[ny_, nx_]
            since_best = 0
        else:
            since_best += 1
            if since_best > stall_steps:
                steepest = True
    return np.array(xs), np.array(ys), 0


@numba.njit(cache=True)
def _carve(
    h: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    bed: np.ndarray,
    sigma: np.ndarray,
) -> None:
    """Lower ``h`` towards the riverbed along a path, with a Gaussian valley cross-section.

    At each path cell, terrain within 3 sigma is lowered to
    ``bed + (h - bed) * (1 - exp(-r^2 / (2 sigma^2)))``: down to the bed on
    the channel line, unchanged far from it. Terrain already below the bed is
    left alone.
    """
    n_y, n_x = h.shape
    for i in range(xs.shape[0]):
        s = sigma[i]
        reach = int(math.ceil(3.0 * s))
        b = bed[i]
        for oy in range(-reach, reach + 1):
            for ox in range(-reach, reach + 1):
                r2 = ox * ox + oy * oy
                if r2 > 9.0 * s * s:
                    continue
                py = (ys[i] + oy) % n_y
                px = (xs[i] + ox) % n_x
                above = h[py, px] - b
                if above > 0.0:
                    target = b + above * (1.0 - math.exp(-r2 / (2.0 * s * s)))
                    if target < h[py, px]:
                        h[py, px] = target


@numba.njit(cache=True)
def _carve_rivers(
    h: np.ndarray,
    sea: np.ndarray,
    sea_level: float,
    src_x: np.ndarray,
    src_y: np.ndarray,
    ux: np.ndarray,
    uy: np.ndarray,
    rdy: np.ndarray,
    rdx: np.ndarray,
    distance: np.ndarray,
    area: np.ndarray,
    seed: int,
    kappa: float,
    inertia: float,
    concavity: float,
    valley_width: float,
    min_valley_sigma: float,
    stall_steps: int,
) -> tuple[np.ndarray, int, int]:
    """Walk and carve every river in turn; returns (river catchment areas, rivers carved, rivers reaching the sea).

    The first result holds each river cell's catchment area (in cells) and 0
    elsewhere.
    """
    np.random.seed(seed)
    n_y, n_x = h.shape
    river = np.zeros((n_y, n_x), dtype=np.bool_)
    river_area = np.zeros((n_y, n_x))
    river_bed = np.zeros((n_y, n_x))
    carved = 0
    to_sea = 0
    max_steps = 4 * (n_x + n_y)
    for r in range(src_x.shape[0]):
        sx = src_x[r]
        sy = src_y[r]
        if sea[sy, sx] or river[sy, sx]:
            continue
        xs, ys, end = _walk(sx, sy, ux, uy, rdy, rdx, distance, sea, river, kappa, inertia, stall_steps, max_steps)
        n = xs.shape[0]
        if end == 0 or n < 3:
            continue
        # Mouth level: the sea, or the existing riverbed at the junction.
        mouth = sea_level if end == 1 else river_bed[ys[n - 1], xs[n - 1]]
        top = h[sy, sx]
        if top <= mouth:
            continue
        # Graded profile from the mouth upstream: bed rises by k * A^-concavity per cell of path length.
        weights = np.zeros(n)
        total = 0.0
        for i in range(n - 2, -1, -1):
            step = math.sqrt(float((xs[i + 1] - xs[i] + n_x // 2) % n_x - n_x // 2) ** 2
                             + float((ys[i + 1] - ys[i] + n_y // 2) % n_y - n_y // 2) ** 2)
            a = max(area[ys[i], xs[i]], 1.0)
            total += step * a ** (-concavity)
            weights[i] = total
        k = (top - mouth) / total
        bed = np.empty(n)
        sigma = np.empty(n)
        for i in range(n):
            bed[i] = mouth + k * weights[i]
            sigma[i] = max(valley_width * math.sqrt(max(area[ys[i], xs[i]], 1.0)), min_valley_sigma)
        # Never above the terrain, and strictly descending downstream.
        for i in range(n):
            bed[i] = min(bed[i], h[ys[i], xs[i]])
            if i > 0:
                bed[i] = min(bed[i], bed[i - 1] - 1e-7)
        # The last cell is the sea or the river being joined: don't carve or mark it.
        last = n - 1
        _carve(h, xs[:last], ys[:last], bed[:last], sigma[:last])
        for i in range(last):
            river[ys[i], xs[i]] = True
            river_area[ys[i], xs[i]] = max(area[ys[i], xs[i]], 1.0)
            river_bed[ys[i], xs[i]] = bed[i]
        carved += 1
        if end == 1:
            to_sea += 1
    return river_area, carved, to_sea


def _smooth_periodic(f: np.ndarray, sigma: float) -> np.ndarray:
    """Gaussian blur of wrap-around grid ``f`` with standard deviation ``sigma`` cells (via FFT)."""
    if sigma <= 0:
        return f
    ky = np.fft.fftfreq(f.shape[0])[:, None]
    kx = np.fft.fftfreq(f.shape[1])[None, :]
    kernel = np.exp(-2.0 * (math.pi * sigma) ** 2 * (kx**2 + ky**2))
    return np.real(np.fft.ifft2(np.fft.fft2(f) * kernel))


def carve_rivers(
    field: np.ndarray,
    sea_level: float,
    params: RiverParams,
    seed: int,
    epsilon: float = 1e-6,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Carve river valleys into a wrap-around height field.

    The field is drained first (``fill_hollows``) so every land cell has a
    downhill path to the sea, then flow is routed to get downstream
    directions and catchment areas, river drunks are walked and carved, and
    the result is drained again. Returns ``(carved_field, river_area,
    rivers_carved, rivers_reaching_sea)``, where ``river_area`` holds each
    river cell's catchment area in cells (at least 1) and 0 elsewhere, so
    ``river_area > 0`` is the river mask. ``field`` and ``sea_level`` are in
    normalised height units; ``field`` is not modified.
    """
    sea = field < sea_level
    h = fill_hollows(field, sea, periodic=True, epsilon=epsilon)
    area, _ = flow_accumulation(h, periodic=True)
    rdy, rdx, distance = _receivers_and_distance(h, sea, _DY, _DX, _DIST)
    # Unit downstream directions, smoothed so drunks follow coherent valleys.
    norm = np.hypot(rdx, rdy).astype(float)
    norm[norm == 0] = 1.0
    ux = _smooth_periodic(rdx / norm, params.direction_smoothing)
    uy = _smooth_periodic(rdy / norm, params.direction_smoothing)

    rng = np.random.default_rng(seed)
    n = h.shape[0]
    pts = poisson_disk_points(params.sources, float(n), rng, periodic=True) + n / 2.0
    sx = np.clip(pts[:, 0].astype(np.int64), 0, n - 1)
    sy = np.clip(pts[:, 1].astype(np.int64), 0, n - 1)
    land_top = float(h.max())
    keep = h[sy, sx] > sea_level + params.min_source_height * (land_top - sea_level)
    sx, sy = sx[keep], sy[keep]
    # Highest sources first, so long trunk rivers are carved before their tributaries.
    order = np.argsort(-h[sy, sx])
    sx, sy = np.ascontiguousarray(sx[order]), np.ascontiguousarray(sy[order])

    carved = h.copy()
    river, n_carved, n_sea = _carve_rivers(
        carved, sea, sea_level, sx, sy, ux, uy, rdy, rdx, distance, area, seed,
        params.kappa, params.inertia, params.concavity, params.valley_width,
        params.min_valley_sigma, params.stall_steps,
    )
    carved = fill_hollows(carved, carved < sea_level, periodic=True, epsilon=epsilon)
    return carved, river, n_carved, n_sea

"""Sampling helpers: Poisson-disk start positions in a square, and power-log parameter spacing."""

import math

import numba
import numpy as np

# Fraction of the square's area covered by a typical Bridson point set, i.e.
# Bridson yields about _BRIDSON_DENSITY * area / r**2 points for radius r.
_BRIDSON_DENSITY = 0.65
# Aim for this many times the requested number of points, so a random subset
# of exactly n can almost always be drawn on the first attempt.
_OVERSAMPLE = 1.3
# Candidate points tried around each active point before it is retired.
_CANDIDATES = 30


@numba.njit(cache=True)
def _far_enough(
    x: float,
    y: float,
    grid: np.ndarray,
    px: np.ndarray,
    py: np.ndarray,
    cell: float,
    side: float,
    radius: float,
    periodic: bool,
) -> bool:
    """True if ``(x, y)`` is at least ``radius`` from every accepted point (measured across the edges if ``periodic``)."""
    cells = grid.shape[0]
    cx = min(int(x / cell), cells - 1)
    cy = min(int(y / cell), cells - 1)
    # A point within `radius` can be at most two cells away in each direction;
    # on a grid narrower than 5 cells that is every cell.
    reach = 2 if cells >= 5 or not periodic else 0
    lo_x, hi_x, lo_y, hi_y = cx - reach, cx + reach + 1, cy - reach, cy + reach + 1
    if not periodic:
        lo_x, hi_x, lo_y, hi_y = max(lo_x, 0), min(hi_x, cells), max(lo_y, 0), min(hi_y, cells)
    elif cells < 5:
        lo_x, hi_x, lo_y, hi_y = 0, cells, 0, cells
    for ix in range(lo_x, hi_x):
        for iy in range(lo_y, hi_y):
            j = grid[ix % cells, iy % cells]
            if j < 0:
                continue
            dx = abs(x - px[j])
            dy = abs(y - py[j])
            if periodic:
                dx = min(dx, side - dx)
                dy = min(dy, side - dy)
            if dx * dx + dy * dy < radius * radius:
                return False
    return True


@numba.njit(cache=True)
def _bridson(side: float, radius: float, periodic: bool, seed: int) -> np.ndarray:
    """Bridson's algorithm in compiled code; returns the points in ``[0, side)^2`` (see ``bridson``)."""
    np.random.seed(seed)
    if periodic:
        # A whole number of cells per side, so neighbouring cells wrap exactly.
        cells = max(1, int(math.floor(side / (radius / math.sqrt(2.0)))))
        cell = side / cells
    else:
        cell = radius / math.sqrt(2.0)
        cells = max(1, int(math.ceil(side / cell)))
    grid = -np.ones((cells, cells), dtype=np.int64)
    # At most one point per background cell.
    px = np.empty(cells * cells)
    py = np.empty(cells * cells)
    active = np.empty(cells * cells, dtype=np.int64)
    n = 0
    n_active = 0
    x = np.random.uniform(0.0, side)
    y = np.random.uniform(0.0, side)
    while True:
        # Accept (x, y): record it in the background grid and mark it active.
        px[n] = x
        py[n] = y
        grid[min(int(x / cell), cells - 1), min(int(y / cell), cells - 1)] = n
        active[n_active] = n
        n += 1
        n_active += 1
        found = False
        while n_active > 0 and not found:
            k = np.random.randint(0, n_active)
            base = active[k]
            for _ in range(_CANDIDATES):
                angle = np.random.uniform(0.0, 2.0 * math.pi)
                dist = np.random.uniform(radius, 2.0 * radius)
                x = px[base] + dist * math.cos(angle)
                y = py[base] + dist * math.sin(angle)
                if periodic:
                    x %= side
                    y %= side
                if 0.0 <= x < side and 0.0 <= y < side and _far_enough(x, y, grid, px, py, cell, side, radius, periodic):
                    found = True
                    break
            if not found:
                # No room left around this point: retire it.
                n_active -= 1
                active[k] = active[n_active]
        if not found:
            break
    points = np.empty((n, 2))
    points[:, 0] = px[:n]
    points[:, 1] = py[:n]
    return points


def bridson(side: float, radius: float, rng: np.random.Generator, periodic: bool = False) -> np.ndarray:
    """Poisson-disk sample the square ``[-side/2, side/2]^2`` with minimum spacing ``radius``.

    Implements Bridson's algorithm: starting from one random point, keep
    proposing candidates in the annulus ``[radius, 2 * radius]`` around active
    points, accepting those at least ``radius`` from every accepted point. A
    background grid with cells of size about ``radius / sqrt(2)`` (at most one
    point per cell) makes each distance check constant-time. With
    ``periodic``, the square wraps around: candidates leaving one edge
    re-enter at the opposite one, and spacing is measured across the edges,
    so the points tile seamlessly. Runs in compiled code, seeded from
    ``rng``. Returns an (N, 2) array of points centred on the origin.
    """
    return _bridson(float(side), float(radius), bool(periodic), int(rng.integers(0, 2**32))) - side / 2.0


def poisson_disk_points(n: int, side: float, rng: np.random.Generator, periodic: bool = False) -> np.ndarray:
    """Return exactly ``n`` Poisson-disk distributed points in the square of side ``side`` centred on the origin.

    The minimum spacing is chosen from ``n`` and the area so that Bridson's
    algorithm yields somewhat more than ``n`` points; a random subset of
    ``n`` is then kept. Any subset still respects the minimum spacing, and
    unlike Bridson's own first ``n`` points (which cluster around its starting
    point) it is spread over the whole square. If too few points are produced,
    the spacing is reduced by 10% and sampling repeated. With ``periodic`` the
    square wraps around (see ``bridson``). Returns shape (n, 2).
    """
    if n < 1:
        raise ValueError(f"need at least one point, got {n}")
    radius = side * math.sqrt(_BRIDSON_DENSITY / (_OVERSAMPLE * n))
    while True:
        points = bridson(side, radius, rng, periodic)
        if len(points) >= n:
            return points[rng.choice(len(points), size=n, replace=False)]
        radius *= 0.9


def power_log_spacing(start: float, end: float, n: int, power: float) -> np.ndarray:
    """Return ``n`` values from ``start`` to ``end`` spaced as ``start * (end / start) ** (t ** power)``.

    ``t`` runs evenly from 0 to 1. ``power = 1`` is logarithmic spacing
    (``np.geomspace``); ``power > 1`` crowds more values towards ``start``,
    ``power < 1`` towards ``end``. Both ends must be positive.
    """
    if start <= 0 or end <= 0:
        raise ValueError(f"power-log spacing needs positive ends, got {start} and {end}")
    if power <= 0:
        raise ValueError(f"power-log spacing needs a positive power, got {power}")
    t = np.linspace(0.0, 1.0, n)
    return start * (end / start) ** (t**power)

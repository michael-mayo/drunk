"""Sampling helpers: Poisson-disk start positions in a square, and power-log parameter spacing."""

import math

import numpy as np

# Fraction of the square's area covered by a typical Bridson point set, i.e.
# Bridson yields about _BRIDSON_DENSITY * area / r**2 points for radius r.
_BRIDSON_DENSITY = 0.65
# Aim for this many times the requested number of points, so a random subset
# of exactly n can almost always be drawn on the first attempt.
_OVERSAMPLE = 1.3
# Candidate points tried around each active point before it is retired.
_CANDIDATES = 30


def bridson(side: float, radius: float, rng: np.random.Generator, periodic: bool = False) -> np.ndarray:
    """Poisson-disk sample the square ``[-side/2, side/2]^2`` with minimum spacing ``radius``.

    Implements Bridson's algorithm: starting from one random point, keep
    proposing candidates in the annulus ``[radius, 2 * radius]`` around active
    points, accepting those at least ``radius`` from every accepted point. A
    background grid with cells of size about ``radius / sqrt(2)`` (at most one
    point per cell) makes each distance check constant-time. With
    ``periodic``, the square wraps around: candidates leaving one edge
    re-enter at the opposite one, and spacing is measured across the edges,
    so the points tile seamlessly. Returns an (N, 2) array of points centred
    on the origin.
    """
    if periodic:
        # A whole number of cells per side, so neighbouring cells wrap exactly.
        cells = max(1, math.floor(side / (radius / math.sqrt(2.0))))
        cell = side / cells
    else:
        cell = radius / math.sqrt(2.0)
        cells = max(1, math.ceil(side / cell))
    grid = -np.ones((cells, cells), dtype=int)
    points: list[tuple[float, float]] = []

    def cell_of(p: tuple[float, float]) -> tuple[int, int]:
        """Background-grid cell containing ``p`` (in [0, side)^2 coordinates)."""
        return min(int(p[0] / cell), cells - 1), min(int(p[1] / cell), cells - 1)

    def distance(p: tuple[float, float], q: tuple[float, float]) -> float:
        """Distance between ``p`` and ``q``, the shorter way round if ``periodic``."""
        dx, dy = abs(p[0] - q[0]), abs(p[1] - q[1])
        if periodic:
            dx, dy = min(dx, side - dx), min(dy, side - dy)
        return math.hypot(dx, dy)

    def far_enough(p: tuple[float, float]) -> bool:
        """True if ``p`` is at least ``radius`` from every accepted point."""
        cx, cy = cell_of(p)
        # A point within `radius` can be at most two cells away in each direction.
        if periodic:
            span = min(5, cells)
            xs = {(cx + d) % cells for d in range(-2, 3)} if span == 5 else range(cells)
            ys = {(cy + d) % cells for d in range(-2, 3)} if span == 5 else range(cells)
        else:
            xs = range(max(cx - 2, 0), min(cx + 3, cells))
            ys = range(max(cy - 2, 0), min(cy + 3, cells))
        for ix in xs:
            for iy in ys:
                j = grid[ix, iy]
                if j >= 0 and distance(p, points[j]) < radius:
                    return False
        return True

    def accept(p: tuple[float, float]) -> None:
        """Record ``p`` as a sample and mark it active."""
        grid[cell_of(p)] = len(points)
        points.append(p)
        active.append(len(points) - 1)

    active: list[int] = []
    accept((rng.uniform(0, side), rng.uniform(0, side)))
    while active:
        k = int(rng.integers(len(active)))
        base = points[active[k]]
        for _ in range(_CANDIDATES):
            angle = rng.uniform(0.0, 2.0 * math.pi)
            dist = rng.uniform(radius, 2.0 * radius)
            p = (base[0] + dist * math.cos(angle), base[1] + dist * math.sin(angle))
            if periodic:
                p = (p[0] % side, p[1] % side)
            if 0 <= p[0] < side and 0 <= p[1] < side and far_enough(p):
                accept(p)
                break
        else:
            # No room left around this point.
            active.pop(k)
    return np.array(points) - side / 2.0


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

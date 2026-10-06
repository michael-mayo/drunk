"""Drainage: fill hollows so every cell drains to an outlet, and route flow downhill.

Both work on wrap-around maps (``periodic=True``, as generated) or on ordinary
cut-out grids, whose edges then act as outlets (as for real-terrain crops).
"""

import heapq

import numba
import numpy as np

# The 8 neighbour offsets (dy, dx) and their distances in cells.
_DY = np.array([-1, -1, -1, 0, 0, 1, 1, 1], dtype=np.int64)
_DX = np.array([-1, 0, 1, -1, 1, -1, 0, 1], dtype=np.int64)
_DIST = np.sqrt(_DY.astype(np.float64) ** 2 + _DX.astype(np.float64) ** 2)


@numba.njit(cache=True)
def _neighbour(y: int, x: int, k: int, n_y: int, n_x: int, periodic: bool, dy: np.ndarray, dx: np.ndarray) -> tuple[int, int]:
    """Index of neighbour ``k`` of ``(y, x)``; ``(-1, -1)`` if it lies off a non-periodic grid."""
    ny = y + dy[k]
    nx = x + dx[k]
    if periodic:
        return ny % n_y, nx % n_x
    if ny < 0 or ny >= n_y or nx < 0 or nx >= n_x:
        return -1, -1
    return ny, nx


@numba.njit(cache=True)
def _priority_flood(h: np.ndarray, outlets: np.ndarray, periodic: bool, epsilon: float, dy: np.ndarray, dx: np.ndarray) -> np.ndarray:
    """Priority-flood fill (Barnes et al. 2014): returns ``h`` with every hollow raised to drain to an outlet."""
    n_y, n_x = h.shape
    out = h.copy()
    done = np.zeros((n_y, n_x), dtype=np.bool_)
    heap = [(0.0, 0, 0)]
    heap.pop()
    for y in range(n_y):
        for x in range(n_x):
            edge = not periodic and (y == 0 or x == 0 or y == n_y - 1 or x == n_x - 1)
            if outlets[y, x] or edge:
                done[y, x] = True
                heapq.heappush(heap, (out[y, x], y, x))
    while heap:
        z, y, x = heapq.heappop(heap)
        for k in range(8):
            ny, nx = _neighbour(y, x, k, n_y, n_x, periodic, dy, dx)
            if ny < 0 or done[ny, nx]:
                continue
            done[ny, nx] = True
            # Raise a neighbour lying in a hollow to just above the spill level, so it drains.
            if out[ny, nx] <= z + epsilon:
                out[ny, nx] = z + epsilon
            heapq.heappush(heap, (out[ny, nx], ny, nx))
    return out


def fill_hollows(field: np.ndarray, outlets: np.ndarray, periodic: bool, epsilon: float = 1e-6) -> np.ndarray:
    """Raise every hollow so each cell has a downhill path to an outlet; returns a new field.

    ``outlets`` marks cells water can leave by (for example the sea). On a
    non-periodic grid the edges are outlets too. A filled hollow becomes a
    surface rising by ``epsilon`` per cell towards its spill point, so flow
    can still be routed across it. ``epsilon`` is in the field's height units.
    """
    if periodic and not outlets.any():
        raise ValueError("a wrap-around map needs at least one outlet (e.g. sea) to drain to")
    return _priority_flood(np.asarray(field, dtype=np.float64), outlets.astype(np.bool_), periodic, epsilon, _DY, _DX)


@numba.njit(cache=True)
def _flow_accumulation(h: np.ndarray, periodic: bool, dy: np.ndarray, dx: np.ndarray, dist: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """D8 flow routing: returns (catchment area in cells, slope to the downstream cell)."""
    n_y, n_x = h.shape
    area = np.ones((n_y, n_x))
    slope = np.zeros((n_y, n_x))
    order = np.argsort(-h.ravel())
    for idx in order:
        y = idx // n_x
        x = idx % n_x
        best = 0.0
        by, bx = -1, -1
        for k in range(8):
            ny, nx = _neighbour(y, x, k, n_y, n_x, periodic, dy, dx)
            if ny < 0:
                continue
            s = (h[y, x] - h[ny, nx]) / dist[k]
            if s > best:
                best, by, bx = s, ny, nx
        slope[y, x] = best
        if by >= 0:
            area[by, bx] += area[y, x]
    return area, slope


def flow_accumulation(field: np.ndarray, periodic: bool) -> tuple[np.ndarray, np.ndarray]:
    """Route flow to each cell's steepest downhill neighbour (D8).

    Returns ``(area, slope)``: the number of cells draining through each cell
    (including itself), and the slope to its downstream neighbour in height
    units per cell (0 where there is no downhill neighbour). The field should
    already be hollow-free (see ``fill_hollows``) for areas to be meaningful.
    """
    return _flow_accumulation(np.asarray(field, dtype=np.float64), periodic, _DY, _DX, _DIST)

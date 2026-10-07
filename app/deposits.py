"""Oriented, anisotropic 2D Gaussian "deposits" left behind by drunks, and their summed field on a grid.

Deposits are stored as flat arrays (one entry per deposit), and summed on a
regular grid by a parallel numba kernel. Each Gaussian is unnormalised: its
value at offset ``d`` from its centre is
``amplitude * exp(-0.5 * (u**2 / var_major + v**2 / var_minor))``, where
``(u, v)`` is ``d`` expressed in the axes rotated by ``angle``.
"""

import math
from dataclasses import dataclass

import numba
import numpy as np


@dataclass(frozen=True)
class Deposits:
    """A set of Gaussian deposits, as parallel 1D arrays of equal length."""

    x: np.ndarray
    y: np.ndarray
    # Rotation of the principal axes, in radians.
    angle: np.ndarray
    var_major: np.ndarray
    var_minor: np.ndarray
    # Peak height at the centre (not the total mass).
    amplitude: np.ndarray

    def __len__(self) -> int:
        """Number of deposits."""
        return len(self.x)


@numba.njit(cache=True)
def _window(c: float, half: float, origin: float, spacing: float, n: int, periodic: bool) -> tuple[int, int]:
    """Grid indices ``[lo, hi)`` whose coordinates lie within ``half`` of ``c``.

    On a periodic grid the indices are left unwrapped (they may fall outside
    ``[0, n)``, and a window wider than the grid covers some cells twice, once
    per periodic copy); otherwise they are clipped to the grid.
    """
    lo = math.ceil((c - half - origin) / spacing)
    hi = math.floor((c + half - origin) / spacing) + 1
    if not periodic:
        lo = max(lo, 0)
        hi = min(hi, n)
    return lo, hi


@numba.njit(parallel=True, cache=True)
def _deposit_field(
    x: np.ndarray,
    y: np.ndarray,
    angle: np.ndarray,
    var_major: np.ndarray,
    var_minor: np.ndarray,
    amplitude: np.ndarray,
    origin: float,
    spacing: float,
    n: int,
    periodic: bool,
    m: float,
) -> np.ndarray:
    """Sum of the deposits on the ``n`` x ``n`` grid ``origin + spacing * i``, each truncated at ``m`` standard deviations.

    The deposits are first bucketed by the grid rows they touch, then rows are
    filled in parallel. Each row is written by one thread only, and adds its
    deposits in their original order, so the result doesn't depend on the
    number of threads.
    """
    count = x.shape[0]
    cos_a = np.empty(count)
    sin_a = np.empty(count)
    half_x = np.empty(count)
    row_lo = np.empty(count, dtype=np.int64)
    row_hi = np.empty(count, dtype=np.int64)
    for d in numba.prange(count):
        c = math.cos(angle[d])
        s = math.sin(angle[d])
        cos_a[d] = c
        sin_a[d] = s
        # m times the square root of the covariance diagonal: the bounding box of the cutoff ellipse.
        half_x[d] = m * math.sqrt(var_major[d] * c * c + var_minor[d] * s * s)
        half_y = m * math.sqrt(var_major[d] * s * s + var_minor[d] * c * c)
        row_lo[d], row_hi[d] = _window(y[d], half_y, origin, spacing, n, periodic)

    # Bucket (deposit, unwrapped row) pairs by wrapped row, in deposit order (compressed sparse rows).
    offsets = np.zeros(n + 1, dtype=np.int64)
    for d in range(count):
        for k in range(row_lo[d], row_hi[d]):
            offsets[k % n + 1] += 1
    for r in range(n):
        offsets[r + 1] += offsets[r]
    entry_deposit = np.empty(offsets[n], dtype=np.int32)
    entry_row = np.empty(offsets[n], dtype=np.int32)
    fill = offsets[:n].copy()
    for d in range(count):
        for k in range(row_lo[d], row_hi[d]):
            r = k % n
            entry_deposit[fill[r]] = d
            entry_row[fill[r]] = k
            fill[r] += 1

    field = np.zeros((n, n))
    for r in numba.prange(n):
        for e in range(offsets[r], offsets[r + 1]):
            d = entry_deposit[e]
            dy = origin + entry_row[e] * spacing - y[d]
            lo, hi = _window(x[d], half_x[d], origin, spacing, n, periodic)
            c = cos_a[d]
            s = sin_a[d]
            for i in range(lo, hi):
                dx = origin + i * spacing - x[d]
                u = c * dx + s * dy
                v = -s * dx + c * dy
                field[r, i % n] += amplitude[d] * math.exp(-0.5 * (u * u / var_major[d] + v * v / var_minor[d]))
    return field


def deposit_field(deposits: Deposits, origin: float, spacing: float, n: int, cutoff: float, periodic: bool) -> np.ndarray:
    """Sum of ``deposits`` on the square grid with coordinates ``origin + spacing * i``, ``i = 0 .. n - 1``.

    Returns shape ``(n, n)``, indexed ``[y, x]``. Each Gaussian is only
    evaluated on the grid points inside the bounding box of the ellipse where
    it falls to ``cutoff`` times its peak, ``m = sqrt(2 ln(1 / cutoff))``
    standard deviations from its centre; everything skipped is below
    ``cutoff * amplitude``.

    With ``periodic``, the grid is one tile of a map that wraps around in both
    directions with period ``n * spacing`` (the grid leaves out the far edge,
    which is the same line as the near edge): a deposit anywhere on the plane
    lands at its position modulo the period, and a bump crossing one edge
    continues across the opposite one.
    """
    if not 0 < cutoff < 1:
        raise ValueError(f"cutoff must be in (0, 1), got {cutoff}")
    m = math.sqrt(2.0 * math.log(1.0 / cutoff))
    return _deposit_field(
        deposits.x, deposits.y, deposits.angle, deposits.var_major, deposits.var_minor, deposits.amplitude,
        float(origin), float(spacing), int(n), bool(periodic), m,
    )

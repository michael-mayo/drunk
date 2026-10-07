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

    Each deposit is bucketed by the first grid row it touches, and copied
    into bucket order so rows read their deposits sequentially. Rows are then
    filled in parallel, each scanning only the buckets of the rows above it
    that a deposit could reach down from. Within a row only the cells inside
    the deposit's cutoff ellipse are visited, and their values come from a
    recurrence (two multiplications per cell) rather than an ``exp`` each.
    Each row is written by one thread only, adding its deposits in a fixed
    order, so the result doesn't depend on the number of threads.
    """
    count = x.shape[0]
    # The exponent is -0.5 * (qa dx^2 + 2 qb dx dy + qc dy^2): the rotated Gaussian's quadratic form.
    qa = np.empty(count)
    qb = np.empty(count)
    qc = np.empty(count)
    row_lo = np.empty(count, dtype=np.int64)
    row_hi = np.empty(count, dtype=np.int64)
    for d in numba.prange(count):
        c = math.cos(angle[d])
        s = math.sin(angle[d])
        qa[d] = c * c / var_major[d] + s * s / var_minor[d]
        qb[d] = c * s * (1.0 / var_major[d] - 1.0 / var_minor[d])
        qc[d] = s * s / var_major[d] + c * c / var_minor[d]
        # m times the square root of the covariance's y entry: the cutoff ellipse's half-height.
        half_y = m * math.sqrt(var_major[d] * s * s + var_minor[d] * c * c)
        row_lo[d], row_hi[d] = _window(y[d], half_y, origin, spacing, n, periodic)

    # Bucket deposits by first row (wrapped), in deposit order (compressed sparse rows);
    # span is the most rows any deposit covers, so row r need only look back span - 1 buckets.
    offsets = np.zeros(n + 1, dtype=np.int64)
    span = 0
    for d in range(count):
        if row_hi[d] > row_lo[d]:
            offsets[row_lo[d] % n + 1] += 1
            span = max(span, row_hi[d] - row_lo[d])
    for r in range(n):
        offsets[r + 1] += offsets[r]
    # Each deposit's numbers in bucket order: x, y, qa, qb, qc, amplitude, and
    # exp(-qa h^2), the factor by which the ratio between neighbouring cells' values shrinks per cell.
    h = spacing
    rec = np.empty((offsets[n], 7))
    rec_lo = np.empty(offsets[n], dtype=np.int64)
    rec_hi = np.empty(offsets[n], dtype=np.int64)
    fill = offsets[:n].copy()
    for d in range(count):
        if row_hi[d] > row_lo[d]:
            q = row_lo[d] % n
            e = fill[q]
            fill[q] += 1
            rec[e, 0] = x[d]
            rec[e, 1] = y[d]
            rec[e, 2] = qa[d]
            rec[e, 3] = qb[d]
            rec[e, 4] = qc[d]
            rec[e, 5] = amplitude[d]
            rec[e, 6] = math.exp(-qa[d] * h * h)
            rec_lo[e] = row_lo[d]
            rec_hi[e] = row_hi[d]
    # Buckets to scan per row: a window wider than the grid can reach a row from any bucket.
    look = min(span, n)
    m2 = m * m

    field = np.zeros((n, n))
    for r in numba.prange(n):
        for j in range(look - 1, -1, -1):
            q = r - j
            if q < 0:
                if not periodic:
                    continue
                q += n
            for e in range(offsets[q], offsets[q + 1]):
                cx = rec[e, 0]
                a = rec[e, 2]
                b = rec[e, 3]
                c = rec[e, 4]
                # Every unwrapped row k of the window that lands on row r (more than one if it wraps round).
                k = rec_lo[e] + (r - rec_lo[e]) % n
                while k < rec_hi[e]:
                    dy = origin + k * spacing - rec[e, 1]
                    # The row's chord through the cutoff ellipse: a dx^2 + 2 b dy dx + c dy^2 <= m^2.
                    disc = b * b * dy * dy - a * (c * dy * dy - m2)
                    if disc >= 0.0:
                        lo, hi = _window(cx - b * dy / a, math.sqrt(disc) / a, origin, spacing, n, periodic)
                        dx = origin + lo * spacing - cx
                        g = rec[e, 5] * math.exp(-0.5 * (a * dx * dx + 2.0 * b * dx * dy + c * dy * dy))
                        ratio = math.exp(-0.5 * (a * (2.0 * dx * h + h * h) + 2.0 * b * dy * h))
                        shrink = rec[e, 6]
                        for i in range(lo, hi):
                            field[r, i % n] += g
                            g *= ratio
                            ratio *= shrink
                    k += n
    return field


def deposit_field(deposits: Deposits, origin: float, spacing: float, n: int, cutoff: float, periodic: bool) -> np.ndarray:
    """Sum of ``deposits`` on the square grid with coordinates ``origin + spacing * i``, ``i = 0 .. n - 1``.

    Returns shape ``(n, n)``, indexed ``[y, x]``. Each Gaussian is only
    evaluated on the grid points inside the ellipse where
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

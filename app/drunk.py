"""Drunkard's walks: gangs of random walkers that leave trails of Gaussian bumps on a wrap-around map.

A drunk starts at its home and staggers one step of ``step_km`` per step in a
random direction, biased back towards home: directions follow a von Mises
distribution centred on the bearing home, with concentration
``kappa_max * (1 - exp(-r / r0))`` at distance ``r`` from home (``r0`` is
``R0_STEPS`` steps). After each step it deposits a Gaussian bump with a random
orientation, standard deviation ``step_km`` along its major axis and
``step_km * sqrt(u)`` (``u`` uniform in (0, 1]) along its minor axis. The k-th
bump has height ``DECAY ** k``.

A ``Gang`` is a group of drunks sharing a step length. Walking and depositing
run together in one parallel numba kernel, so no deposit list is ever stored.
"""

import math
from dataclasses import dataclass

import numba
import numpy as np

# Steps each drunk takes.
STEPS = 1000
# Height of the k-th bump is DECAY ** k.
DECAY = 0.999
# Distance (in steps) over which the homeward bias builds up.
R0_STEPS = 10.0
# Bumps are truncated this many standard deviations from their centre.
CUTOFF_SD = 4.0
# Drunks are split into this many groups, each summed on its own grid, so the result doesn't depend on thread count.
CHUNKS = 16
# A gang is rendered on a grid with at least this many cells per bump standard deviation, then upsampled.
CELLS_PER_SD = 1.5


@numba.njit(cache=True)
def _splat(grid: np.ndarray, x: float, y: float, angle: float, sd_major: float, sd_minor: float, amp: float) -> None:
    """Add a rotated Gaussian bump centred on ``(x, y)`` (in cells) to the wrap-around ``grid``."""
    n = grid.shape[0]
    c, s = math.cos(angle), math.sin(angle)
    ia, ib = 1.0 / sd_major**2, 1.0 / sd_minor**2
    qa, qb, qc = c * c * ia + s * s * ib, c * s * (ia - ib), s * s * ia + c * c * ib
    r = int(CUTOFF_SD * sd_major) + 1
    ix, iy = int(round(x)), int(round(y))
    for i in range(iy - r, iy + r + 1):
        dy = i - y
        row = grid[i % n]
        for j in range(ix - r, ix + r + 1):
            dx = j - x
            q = qa * dx * dx + 2.0 * qb * dx * dy + qc * dy * dy
            if q < CUTOFF_SD * CUTOFF_SD:
                row[j % n] += amp * math.exp(-0.5 * q)


@numba.njit(parallel=True, cache=True)
def _walk(seeds: np.ndarray, homes: np.ndarray, kappa_max: np.ndarray, step: float, n: int) -> np.ndarray:
    """Walk every drunk and sum its bumps on an ``n`` x ``n`` wrap-around grid; positions are in cells."""
    grids = np.zeros((CHUNKS, n, n))
    r0 = R0_STEPS * step
    for chunk in numba.prange(CHUNKS):
        for d in range(chunk, len(seeds), CHUNKS):
            np.random.seed(seeds[d])
            hx, hy = homes[d, 0], homes[d, 1]
            x, y, amp = hx, hy, 1.0
            for _ in range(STEPS):
                kappa = kappa_max[d] * (1.0 - math.exp(-math.hypot(x - hx, y - hy) / r0))
                heading = np.random.vonmises(math.atan2(hy - y, hx - x), kappa)
                x += step * math.cos(heading)
                y += step * math.sin(heading)
                angle = np.random.uniform(0.0, math.pi)
                _splat(grids[chunk], x, y, angle, step, step * math.sqrt(1.0 - np.random.random()), amp)
                amp *= DECAY
    return grids.sum(axis=0)


def _upsample(field: np.ndarray, n: int) -> np.ndarray:
    """Band-limited (Fourier) upsampling of a square wrap-around field to ``n`` x ``n``."""
    m = field.shape[0]
    if m == n:
        return field
    spectrum = np.fft.rfft2(field)
    padded = np.zeros((n, n // 2 + 1), dtype=complex)
    h = m // 2
    padded[:h, :h] = spectrum[:h, :h]
    padded[n - h + 1 :, :h] = spectrum[h + 1 :, :h]
    return np.fft.irfft2(padded, s=(n, n)) * (n / m) ** 2


@dataclass(frozen=True, eq=False)
class Gang:
    """Drunks sharing a step length: one seed each, homes in km (``(n, 2)``) and homeward biases."""

    seeds: np.ndarray
    homes_km: np.ndarray
    kappa_max: np.ndarray
    step_km: float

    def __len__(self) -> int:
        """Number of drunks."""
        return len(self.seeds)

    def field(self, side_km: float, n: int) -> np.ndarray:
        """The gang's summed bumps on an ``n`` x ``n`` grid wrapping round a square ``side_km`` across.

        Big bumps are smooth, so the walk is rendered on the coarsest grid
        that still has ``CELLS_PER_SD`` cells per bump and upsampled.
        """
        m = min(n, 2 * math.ceil(CELLS_PER_SD * side_km / self.step_km / 2))
        cell_km = side_km / m
        coarse = _walk(self.seeds.astype(np.int64), self.homes_km / cell_km, self.kappa_max.astype(np.float64),
                       self.step_km / cell_km, m)
        return _upsample(coarse, n)

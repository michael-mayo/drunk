"""Alluvial plains: bury the low ground of a wrap-around map under smooth plains that rise gently from the coast.

Drunks only ever add bumps, so on their own they leave lowlands as rough as
mountains. Real coasts and basins are the opposite: valleys between ranges fill
with sediment into flats, and the ranges rise sharply out of them. A ``Plains``
step does that to a height field:

- the plain is the land whose height, smoothed over ``smooth_km``, is below the
  mountain front at ``front`` of the land's relief above sea level;
- there the surface is replaced by one rising from the coast towards ``plain``
  of the relief over about ``length_km`` (``1 - exp(-distance / length_km)``),
  keeping ``keep`` of the original small relief;
- the change fades out over the top 40% of the band below the front, so the
  ranges themselves are untouched and meet the plain at a sharp foot.

Sea cells are never changed and land stays above sea level, so the share of the
map under the sea is unchanged.
"""

import math
from dataclasses import dataclass

import numba
import numpy as np

# Share of the band below the mountain front over which the plain blends into the untouched ranges.
BLEND = 0.4


@numba.njit(cache=True)
def _distance(source: np.ndarray) -> np.ndarray:
    """Chamfer distance in cells from every cell to the nearest True cell of the wrap-around ``source``.

    Two forward and backward raster passes; the second pair carries
    distances across the wrap-around seams.
    """
    n = source.shape[0]
    d = np.where(source, 0.0, 1e12)
    diagonal = math.sqrt(2.0)
    for _ in range(2):
        for i in range(n):
            for j in range(n):
                d[i, j] = min(d[i, j], d[i - 1, j] + 1.0, d[i, j - 1] + 1.0,
                              d[i - 1, j - 1] + diagonal, d[i - 1, (j + 1) % n] + diagonal)
        for i in range(n - 1, -1, -1):
            for j in range(n - 1, -1, -1):
                d[i, j] = min(d[i, j], d[(i + 1) % n, j] + 1.0, d[i, (j + 1) % n] + 1.0,
                              d[(i + 1) % n, (j + 1) % n] + diagonal, d[(i + 1) % n, j - 1] + diagonal)
    return d


def _blur(z: np.ndarray, sd_cells: float) -> np.ndarray:
    """Gaussian blur (standard deviation ``sd_cells``) of a square wrap-around field."""
    k = np.fft.fftfreq(z.shape[0])
    gain = np.exp(-2.0 * (math.pi * sd_cells) ** 2 * (k[:, None] ** 2 + k[None, :] ** 2))
    return np.fft.ifft2(np.fft.fft2(z) * gain).real


@dataclass(frozen=True)
class Plains:
    """An alluvial-plain step: mountain front and plain height (shares of the land's relief), rise length, kept relief."""

    front: float
    plain: float
    length_km: float
    keep: float
    smooth_km: float

    def apply(self, height: np.ndarray, sea_fraction: float, side_km: float) -> np.ndarray:
        """``height`` (a wrap-around square ``side_km`` across) with its low land under plains; sea stays as it is."""
        cell_km = side_km / height.shape[0]
        sea_level = np.quantile(height, sea_fraction)
        sea = height <= sea_level
        relief = np.percentile(height[~sea], 99) - sea_level
        # 1 on the plain, 0 in the ranges, a smoothstep between.
        smoothed = _blur(height, self.smooth_km / cell_km)
        t = np.clip((smoothed - sea_level - (1.0 - BLEND) * self.front * relief) / (BLEND * self.front * relief), 0.0, 1.0)
        on_plain = 1.0 - t * t * (3.0 - 2.0 * t)
        # With no sea, the plain rises from the lowest point.
        rise = 1.0 - np.exp(-_distance(sea) * cell_km / self.length_km)
        surface = sea_level + self.plain * relief * rise
        flattened = surface + self.keep * (height - surface)
        return np.where(sea, height, on_plain * flattened + (1.0 - on_plain) * height)

    def describe(self) -> str:
        """A short description, like a gang's."""
        return (f"plains up to {self.front:.0%} of relief, rising to {self.plain:.0%} over {self.length_km:.0f} km, "
                f"{self.keep:.0%} relief kept")

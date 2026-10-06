"""Oriented, anisotropic 2D Gaussian "deposits" left behind by a drunk."""

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class GaussianDeposit:
    """An unnormalised 2D Gaussian bump with arbitrary orientation.

    The bump is centred at ``center`` and has principal axes rotated by
    ``angle`` radians, with variances ``var_major`` and ``var_minor`` along
    them. ``amplitude`` is the peak height at the centre (not the total mass),
    so the value at offset ``d`` is
    ``amplitude * exp(-0.5 * (u**2 / var_major + v**2 / var_minor))``, where
    ``(u, v)`` is ``d`` expressed in the rotated axes.
    """

    center: tuple[float, float]
    angle: float
    var_major: float
    var_minor: float
    amplitude: float

    def evaluate(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        """Evaluate the bump at coordinates ``xs``, ``ys`` (arrays of matching shape)."""
        dx = xs - self.center[0]
        dy = ys - self.center[1]
        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        # Rotate offsets into the Gaussian's principal-axis frame.
        u = cos_a * dx + sin_a * dy
        v = -sin_a * dx + cos_a * dy
        return self.amplitude * np.exp(-0.5 * (u**2 / self.var_major + v**2 / self.var_minor))

    def add_to_grid(
        self,
        field: np.ndarray,
        gx: np.ndarray,
        gy: np.ndarray,
        cutoff: float,
        period: float | None = None,
    ) -> None:
        """Add the bump into ``field`` (shape ``(len(gy), len(gx))``) on the regular grid ``gx`` x ``gy``.

        Only the grid points inside the bump's axis-aligned bounding box are
        evaluated: the box encloses the ellipse where the bump falls to
        ``cutoff`` times its peak, i.e. ``m = sqrt(2 ln(1 / cutoff))``
        standard deviations from the centre. Everything outside it is below
        ``cutoff * amplitude`` and is skipped. ``cutoff <= 0`` evaluates the
        whole grid (only allowed without ``period``). ``field`` is modified in place.

        With ``period``, the grid is one tile of a map that wraps around in
        both directions (the grid should cover ``[gx[0], gx[0] + period)``
        without repeating the far edge, likewise for ``gy``). The bump's
        centre is wrapped into the tile, and every periodic copy whose window
        overlaps the tile is added, so a bump crossing one edge continues
        across the opposite edge.
        """
        if period is not None:
            if cutoff <= 0:
                raise ValueError("a periodic grid needs cutoff > 0, so each bump has a finite window")
            cx = gx[0] + (self.center[0] - gx[0]) % period
            cy = gy[0] + (self.center[1] - gy[0]) % period
            half_x, half_y = self._window_half_widths(cutoff)
            # Copies one period either side cover any window up to a period wide;
            # wider windows need more.
            kx = math.ceil(half_x / period)
            ky = math.ceil(half_y / period)
            for i in range(-kx, kx + 1):
                for j in range(-ky, ky + 1):
                    self._add_window(field, gx, gy, cx + i * period, cy + j * period, half_x, half_y)
            return
        half_x, half_y = self._window_half_widths(cutoff)
        self._add_window(field, gx, gy, self.center[0], self.center[1], half_x, half_y)

    def _window_half_widths(self, cutoff: float) -> tuple[float, float]:
        """Half-widths of the bounding box of the ellipse where the bump falls to ``cutoff`` times its peak."""
        m = math.sqrt(2.0 * math.log(1.0 / cutoff)) if cutoff > 0 else math.inf
        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        # m * sqrt of the covariance diagonal.
        half_x = m * math.sqrt(self.var_major * cos_a**2 + self.var_minor * sin_a**2)
        half_y = m * math.sqrt(self.var_major * sin_a**2 + self.var_minor * cos_a**2)
        return half_x, half_y

    def _add_window(
        self,
        field: np.ndarray,
        gx: np.ndarray,
        gy: np.ndarray,
        cx: float,
        cy: float,
        half_x: float,
        half_y: float,
    ) -> None:
        """Add the bump, centred at ``(cx, cy)``, to the grid points inside its ``half_x`` x ``half_y`` window."""
        cos_a = math.cos(self.angle)
        sin_a = math.sin(self.angle)
        ix0 = np.searchsorted(gx, cx - half_x, side="left")
        ix1 = np.searchsorted(gx, cx + half_x, side="right")
        iy0 = np.searchsorted(gy, cy - half_y, side="left")
        iy1 = np.searchsorted(gy, cy + half_y, side="right")
        if ix0 >= ix1 or iy0 >= iy1:
            return
        # Broadcast a row of x offsets against a column of y offsets: only the window is materialised.
        dx = gx[ix0:ix1][np.newaxis, :] - cx
        dy = gy[iy0:iy1][:, np.newaxis] - cy
        u = cos_a * dx + sin_a * dy
        v = -sin_a * dx + cos_a * dy
        field[iy0:iy1, ix0:ix1] += self.amplitude * np.exp(-0.5 * (u**2 / self.var_major + v**2 / self.var_minor))

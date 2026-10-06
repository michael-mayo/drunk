"""Scale-free statistics for comparing height fields with natural terrain.

Every statistic here is invariant to the units and overall scale of the
heights (and of the horizontal axes, given a square grid covering the same
fraction of the domain), so model fields in arbitrary units can be compared
directly with real elevation data in metres.
"""

from dataclasses import dataclass

import numpy as np

# Wavenumber band (cycles per domain side) used to fit the spectral slope:
# wavelengths from 1/4 down to 1/64 of the domain.
SPECTRUM_BAND = (4.0, 64.0)
# Lag band, as fractions of the domain side, used to fit the roughness exponent.
LAG_BAND = (1.0 / 64.0, 1.0 / 4.0)


@dataclass(frozen=True)
class TerrainStats:
    """Summary statistics of one height field."""

    # Slope of the radially averaged 2D power spectrum, P(k) ~ k^-beta.
    spectral_slope: float
    # Roughness (Hurst) exponent from the structure function, S(lag) ~ lag^H.
    roughness: float
    # Hypsometric integral: (mean - min) / (max - min), in [0, 1].
    hypsometric_integral: float
    # Skewness of the height distribution.
    skewness: float


def _detrend(z: np.ndarray) -> np.ndarray:
    """Remove the best-fit plane from ``z`` so a regional tilt doesn't dominate the spectrum."""
    ny, nx = z.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    design = np.column_stack([np.ones(z.size), xx.ravel(), yy.ravel()])
    coeffs, *_ = np.linalg.lstsq(design, z.ravel(), rcond=None)
    return z - (design @ coeffs).reshape(z.shape)


def spectral_slope(z: np.ndarray, band: tuple[float, float] = SPECTRUM_BAND) -> float:
    """Fit ``beta`` in ``P(k) ~ k^-beta`` for the radially averaged power spectrum of square field ``z``.

    The field is detrended and multiplied by a 2D Hann window to suppress
    edge leakage. Power is averaged in logarithmic wavenumber bins and a
    straight line fitted in log-log space over ``band`` (cycles per side).
    For fractional Brownian surfaces ``beta = 2H + 2``.
    """
    n = z.shape[0]
    window = np.outer(np.hanning(n), np.hanning(n))
    power = np.abs(np.fft.fftshift(np.fft.fft2(_detrend(z) * window))) ** 2
    ky, kx = np.indices(power.shape) - n // 2
    k = np.hypot(kx, ky)
    edges = np.geomspace(band[0], band[1], 17)
    centres, means = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (k >= lo) & (k < hi)
        if mask.any():
            centres.append(np.sqrt(lo * hi))
            means.append(power[mask].mean())
    slope, _ = np.polyfit(np.log(centres), np.log(means), 1)
    return float(-slope)


def roughness_exponent(z: np.ndarray, lag_band: tuple[float, float] = LAG_BAND) -> float:
    """Fit ``H`` in ``S(lag) ~ lag^H``, where ``S`` is the RMS height difference at a horizontal lag.

    Differences are taken along both axes and pooled. Lags are spaced
    logarithmically across ``lag_band`` (fractions of the domain side).
    """
    n = z.shape[0]
    lags = np.unique(np.geomspace(max(1, lag_band[0] * n), lag_band[1] * n, 12).astype(int))
    rms = []
    for lag in lags:
        dx = z[:, lag:] - z[:, :-lag]
        dy = z[lag:, :] - z[:-lag, :]
        rms.append(np.sqrt((np.mean(dx**2) + np.mean(dy**2)) / 2.0))
    slope, _ = np.polyfit(np.log(lags), np.log(rms), 1)
    return float(slope)


def hypsometric_integral(z: np.ndarray) -> float:
    """Mean height as a fraction of the min-to-max range: 0 = all low, 1 = all high."""
    lo, hi = z.min(), z.max()
    return float((z.mean() - lo) / (hi - lo)) if hi > lo else 0.0


def skewness(z: np.ndarray) -> float:
    """Sample skewness of the heights (positive = long tail of high ground)."""
    d = z - z.mean()
    sd = d.std()
    return float(np.mean(d**3) / sd**3) if sd > 0 else 0.0


def terrain_stats(z: np.ndarray) -> TerrainStats:
    """Compute all statistics for square height field ``z``."""
    z = np.asarray(z, dtype=float)
    if z.ndim != 2 or z.shape[0] != z.shape[1]:
        raise ValueError(f"expected a square 2D field, got shape {z.shape}")
    return TerrainStats(
        spectral_slope=spectral_slope(z),
        roughness=roughness_exponent(z),
        hypsometric_integral=hypsometric_integral(z),
        skewness=skewness(z),
    )


# Fill depth, as a fraction of the field's relief, above which a cell counts as
# lying in a real depression (shallower dents are measurement-scale noise).
DEPRESSION_DEPTH = 1e-3
# Smallest catchment (in cells) counted as a channel for the slope-area fit.
CHANNEL_MIN_AREA = 50
# Channel cells flatter than this (height change per cell, as a fraction of
# the relief) are left out of the fit: filled hollows are near-flat surfaces
# whose tiny drainage gradient would otherwise dominate and destabilise it.
CHANNEL_MIN_SLOPE = 1e-4


@dataclass(frozen=True)
class DrainageStats:
    """Drainage statistics of one height field."""

    # Channel concavity theta in slope ~ area^-theta (natural rivers ~0.4-0.6).
    concavity: float
    # Fraction of land that must be filled by more than DEPRESSION_DEPTH x relief to drain.
    depression_fraction: float


def drainage_stats(z: np.ndarray, outlets: np.ndarray, periodic: bool) -> DrainageStats:
    """Compute drainage statistics for height field ``z``.

    ``outlets`` marks cells water leaves by (sea); on a non-periodic grid the
    edges are outlets too. The field is first filled so every cell drains
    (``app.drainage.fill_hollows``); the depression fraction is the share of
    non-outlet cells raised by more than ``DEPRESSION_DEPTH`` of the relief.
    Flow is then routed (D8) on the filled field, and the concavity is the
    negative slope of a log-log fit of median channel slope against
    catchment area, over logarithmic area bins from ``CHANNEL_MIN_AREA``
    cells up. Near-flat cells (slope below ``CHANNEL_MIN_SLOPE`` x relief per
    cell, such as filled hollows) are excluded, for real and generated
    terrain alike.
    """
    # Imported here so the generation-only statistics don't need numba.
    from app.drainage import fill_hollows
    from app.drainage import flow_accumulation

    z = np.asarray(z, dtype=float)
    relief = float(z.max() - z.min()) or 1.0
    filled = fill_hollows(z, outlets, periodic, epsilon=1e-9 * relief)
    land = ~outlets
    depression_fraction = float(np.mean((filled - z)[land] > DEPRESSION_DEPTH * relief))
    area, slope = flow_accumulation(filled, periodic)
    channel = land & (area >= CHANNEL_MIN_AREA) & (slope > CHANNEL_MIN_SLOPE * relief)
    a, s = area[channel], slope[channel]
    edges = np.geomspace(CHANNEL_MIN_AREA, max(a.max(), CHANNEL_MIN_AREA * 2), 13)
    xs, ys = [], []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (a >= lo) & (a < hi)
        if m.sum() >= 10:
            xs.append(np.log(np.sqrt(lo * hi)))
            ys.append(np.log(np.median(s[m])))
    concavity = float(-np.polyfit(xs, ys, 1)[0]) if len(xs) >= 3 else float("nan")
    return DrainageStats(concavity=concavity, depression_fraction=depression_fraction)

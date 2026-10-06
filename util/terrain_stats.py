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

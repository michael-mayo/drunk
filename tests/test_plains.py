"""Tests for alluvial plains."""

import numpy as np

from app.plains import Plains
from app.plains import _distance

PLAINS = Plains(front=0.3, plain=0.05, length_km=5.0, keep=0.05, smooth_km=1.0)


def hills_by_the_sea() -> np.ndarray:
    """A 128 x 128 wrap-around field: sea on the left, rough low land in the middle, rough mountains on the right."""
    rng = np.random.default_rng(0)
    x = np.mgrid[0:128, 0:128][1] / 128.0
    return 10.0 * np.sin(np.pi * x) ** 4 + 0.3 * rng.normal(size=(128, 128)) + 3.0 * x


def test_distance_wraps_around() -> None:
    source = np.zeros((16, 16), dtype=bool)
    source[0, 0] = True
    d = _distance(source)
    assert d[0, 0] == 0.0 and d[0, 15] == 1.0 and d[15, 15] == np.sqrt(2.0) and d[8, 0] == 8.0


def test_plains_keep_the_sea_and_the_sea_share() -> None:
    z = hills_by_the_sea()
    out = PLAINS.apply(z, 0.3, 10.0)
    sea = z <= np.quantile(z, 0.3)
    np.testing.assert_array_equal(out[sea], z[sea])
    assert (out[~sea] > np.quantile(z, 0.3)).all()


def test_plains_flatten_low_land_and_leave_mountains() -> None:
    z = hills_by_the_sea()
    out = PLAINS.apply(z, 0.3, 10.0)
    sea_level = np.quantile(z, 0.3)
    low = (z > sea_level) & (z < sea_level + 1.0)
    peak = z > np.percentile(z, 95)
    roughness = lambda f, m: np.abs(np.diff(f, axis=0))[m[1:]].mean()
    assert roughness(out, low) < 0.3 * roughness(z, low)
    np.testing.assert_allclose(out[peak], z[peak])

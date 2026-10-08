"""Tests for the drunks' walks and their rendered fields."""

import numpy as np

from app.drunk import Gang
from app.drunk import _upsample


def gang(step_km: float, n: int = 20, seed: int = 0) -> Gang:
    """A reproducible gang of ``n`` drunks spread over a 10 km square."""
    rng = np.random.default_rng(seed)
    return Gang(rng.integers(0, 2**32, n), rng.uniform(0, 10, (n, 2)), rng.uniform(0.01, 1, n), step_km)


def test_field_is_reproducible_and_positive() -> None:
    a, b = gang(0.1).field(10.0, 128), gang(0.1).field(10.0, 128)
    assert a.shape == (128, 128)
    np.testing.assert_array_equal(a, b)
    assert a.min() >= 0 and a.max() > 0


def test_field_wraps_around() -> None:
    """A drunk at the corner leaves bumps on all four corners of the wrap-around grid."""
    field = Gang(np.array([1]), np.array([[0.0, 0.0]]), np.array([5.0]), 0.2).field(10.0, 100)
    corners = [field[0, 0], field[0, -1], field[-1, 0], field[-1, -1]]
    assert min(corners) > 0.1 * field.max()


def test_coarse_gang_is_rendered_coarse_and_upsampled() -> None:
    """Big bumps render on a coarser grid; the upsampled field keeps its mean."""
    field = gang(2.0).field(10.0, 256)
    assert field.shape == (256, 256)
    coarse = np.random.default_rng(1).random((16, 16))
    assert np.isclose(_upsample(coarse, 64).mean(), coarse.mean())

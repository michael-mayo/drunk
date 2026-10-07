"""Tests for summing Gaussian deposits on a grid."""

import numba
import numpy as np
import pytest

from app.deposits import Deposits
from app.deposits import deposit_field

# Grid used by most tests: 64 points over a 32-unit square centred on the origin.
N = 64
SIDE = 32.0
CUTOFF = 1 / 4096


def random_deposits(rng: np.random.Generator, count: int, spread: float) -> Deposits:
    """``count`` random deposits with centres within ``spread`` of the origin."""
    var_major = rng.uniform(0.2, 6.0, count)
    return Deposits(
        x=rng.uniform(-spread, spread, count),
        y=rng.uniform(-spread, spread, count),
        angle=rng.uniform(0.0, np.pi, count),
        var_major=var_major,
        var_minor=var_major * rng.uniform(0.05, 1.0, count),
        amplitude=rng.uniform(0.2, 1.0, count),
    )


def exact_field(d: Deposits, axis: np.ndarray) -> np.ndarray:
    """Every deposit evaluated over the whole grid, with no cutoff."""
    xx, yy = np.meshgrid(axis, axis)
    field = np.zeros_like(xx)
    for i in range(len(d)):
        dx, dy = xx - d.x[i], yy - d.y[i]
        c, s = np.cos(d.angle[i]), np.sin(d.angle[i])
        u, v = c * dx + s * dy, -s * dx + c * dy
        field += d.amplitude[i] * np.exp(-0.5 * (u**2 / d.var_major[i] + v**2 / d.var_minor[i]))
    return field


def test_matches_exact_evaluation_within_cutoff() -> None:
    """Truncating at the cutoff loses at most cutoff x amplitude per deposit."""
    d = random_deposits(np.random.default_rng(0), 200, 12.0)
    axis = np.linspace(-SIDE / 2, SIDE / 2, N)
    field = deposit_field(d, axis[0], axis[1] - axis[0], N, CUTOFF, periodic=False)
    exact = exact_field(d, axis)
    assert np.all(field <= exact + 1e-12)
    assert np.max(exact - field) <= CUTOFF * d.amplitude.sum()


def test_periodic_matches_exact_sum_of_copies_even_for_windows_wider_than_the_map() -> None:
    """On a wrap-around grid each deposit counts once per periodic copy, including bumps wider than the map."""
    rng = np.random.default_rng(5)
    d = random_deposits(rng, 60, 40.0)
    # Make some deposits wide enough that their cutoff window exceeds the 32-unit period.
    wide = Deposits(d.x, d.y, d.angle, d.var_major * np.where(np.arange(60) % 3 == 0, 20.0, 1.0),
                    d.var_minor, d.amplitude)
    axis = -SIDE / 2 + SIDE * np.arange(N) / N
    field = deposit_field(wide, axis[0], SIDE / N, N, CUTOFF, periodic=True)
    exact = np.zeros((N, N))
    for i in range(-3, 4):
        for j in range(-3, 4):
            shifted = Deposits(wide.x + i * SIDE, wide.y + j * SIDE, wide.angle, wide.var_major, wide.var_minor,
                               wide.amplitude)
            exact += exact_field(shifted, axis)
    assert np.all(field <= exact + 1e-9)
    # A window spans under 3 periods, so up to 3 x 3 copies of a deposit reach a point, each truncated.
    assert np.max(exact - field) <= 9 * CUTOFF * wide.amplitude.sum()


def test_periodic_shift_by_one_period_is_unchanged() -> None:
    """A deposit and its copy one period away land in the same place."""
    d = random_deposits(np.random.default_rng(1), 100, 30.0)
    shifted = Deposits(d.x + SIDE, d.y - 2 * SIDE, d.angle, d.var_major, d.var_minor, d.amplitude)
    a = deposit_field(d, -SIDE / 2, SIDE / N, N, CUTOFF, periodic=True)
    b = deposit_field(shifted, -SIDE / 2, SIDE / N, N, CUTOFF, periodic=True)
    np.testing.assert_allclose(a, b, atol=1e-10)


def test_periodic_shift_by_whole_cells_rolls_the_field() -> None:
    """Moving every deposit by whole grid cells rolls the wrap-around field, with nothing lost at the edges."""
    d = random_deposits(np.random.default_rng(2), 100, 16.0)
    cell = SIDE / N
    shifted = Deposits(d.x + 5 * cell, d.y - 9 * cell, d.angle, d.var_major, d.var_minor, d.amplitude)
    a = deposit_field(d, -SIDE / 2, cell, N, CUTOFF, periodic=True)
    b = deposit_field(shifted, -SIDE / 2, cell, N, CUTOFF, periodic=True)
    np.testing.assert_allclose(np.roll(a, (-9, 5), axis=(0, 1)), b, atol=1e-10)


def test_result_does_not_depend_on_thread_count() -> None:
    """Rows are each filled by one thread in a fixed order, so results are bit-for-bit identical."""
    d = random_deposits(np.random.default_rng(3), 500, 40.0)
    default = numba.get_num_threads()
    try:
        numba.set_num_threads(1)
        one = deposit_field(d, -SIDE / 2, SIDE / N, N, CUTOFF, periodic=True)
    finally:
        numba.set_num_threads(default)
    many = deposit_field(d, -SIDE / 2, SIDE / N, N, CUTOFF, periodic=True)
    assert np.array_equal(one, many)


@pytest.mark.parametrize("cutoff", [0.0, 1.0, -0.1])
def test_rejects_cutoff_outside_open_unit_interval(cutoff: float) -> None:
    """A cutoff must give each Gaussian a finite, non-empty window."""
    d = random_deposits(np.random.default_rng(4), 1, 1.0)
    with pytest.raises(ValueError):
        deposit_field(d, -SIDE / 2, SIDE / N, N, cutoff, periodic=True)

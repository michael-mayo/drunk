"""Tests for the realism objective and the greedy map builder."""

import math

import numpy as np

from app.build_map import REFERENCE
from app.build_map import build_map
from app.build_map import nearest
from app.build_map import objective
from app.build_map import stats
from app.build_map import windows


def test_stats_of_a_rough_and_a_smooth_field() -> None:
    """White noise doesn't get rougher with distance; an integrated random walk does."""
    rng = np.random.default_rng(0)
    land = np.ones((256, 256), dtype=bool)
    noise = stats(rng.normal(size=(256, 256)), land)
    walk = stats(rng.normal(size=(256, 256)).cumsum(0).cumsum(1), land)
    assert abs(noise[0]) < 0.1 and abs(noise[1]) < 0.1
    assert walk[0] > 0.3 and walk[1] > 0.3
    assert noise[4] == 0.0


def test_water_is_left_out() -> None:
    """Statistics only see land: a field that is noise on land and huge in the sea measures like the noise."""
    rng = np.random.default_rng(1)
    z = rng.normal(size=(256, 256))
    land = np.ones(z.shape, dtype=bool)
    land[:, :64] = False
    wet = z.copy()
    wet[:, :64] = 1e6
    a, b = stats(z, land), stats(wet, land)
    np.testing.assert_allclose(a, b)
    assert math.isclose(a[4], 0.25)
    assert np.isnan(stats(z, np.zeros(z.shape, dtype=bool))).all()


def test_windows_tile_the_world() -> None:
    a = np.arange(1024 * 1024).reshape(1024, 1024)
    w = windows(a)
    assert w.shape == (16, 256, 256)
    np.testing.assert_array_equal(w[5], a[256:512, 256:512])


def test_drainage_finds_hollows() -> None:
    """A bowl drains nowhere, so most of it is a hollow; a peak and a tilted plane drain to the edges."""
    y, x = np.mgrid[0:128, 0:128] - 63.5
    land = np.ones((128, 128), dtype=bool)
    noise = 0.01 * np.random.default_rng(2).random((128, 128))
    assert stats(np.hypot(x, y) + noise, land)[5] > 0.5
    assert stats(-np.hypot(x, y) + noise, land)[5] < 0.05
    assert stats(x + y + noise, land)[5] < 0.05


def test_flat_map_is_infinitely_bad() -> None:
    assert objective(np.zeros((1024, 1024)), 0.3) == math.inf


def test_build_only_keeps_improvements() -> None:
    history = []
    world = build_map(5, trials=4, progress=lambda _, t, n, best, ok, text: history.append((best, ok)))
    scores = [best for best, _ in history]
    assert scores == sorted(scores, reverse=True)
    assert len(world.gangs) == sum(ok for _, ok in history) >= 1
    assert np.isclose(objective(world.height, world.sea_fraction), scores[-1])


def test_a_set_sea_share_is_kept() -> None:
    """With a sea share given, the builder never changes it, and scores the map at it."""
    shares, history = [], []

    def progress(world, t, n, best, ok, text) -> None:
        shares.append(world.sea_fraction)
        history.append(best)

    world = build_map(5, trials=4, sea_fraction=0.42, progress=progress)
    assert set(shares) == {0.42}
    assert np.isclose(objective(world.height, 0.42), history[-1])


def test_real_squares_are_nearest_to_themselves() -> None:
    """A real square is the nearest to its own statistics, which are closer than shifted ones."""
    names, real = REFERENCE["world"]
    distance, like = nearest(real[0], "world")
    assert like[0] == names[0] and distance < nearest(real[0] + 0.5, "world")[0]
    assert nearest(np.full(real.shape[1], np.nan), "world")[0] == math.inf

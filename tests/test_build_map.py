"""Tests for the realism objective and the greedy map builder."""

import math

import numpy as np

from app.build_map import build_map
from app.build_map import objective
from app.build_map import stats


def test_stats_of_a_rough_and_a_smooth_field() -> None:
    """White noise has a flat spectrum and no roughness growth; an integrated walk is steep and rough."""
    rng = np.random.default_rng(0)
    noise = rng.normal(size=(1, 256, 256))
    walk = rng.normal(size=(1, 256, 256)).cumsum(1).cumsum(2)
    beta_noise, h_noise = stats(noise)[0, :2]
    beta_walk, h_walk = stats(walk)[0, :2]
    assert abs(beta_noise) < 0.3 and abs(h_noise) < 0.1
    assert beta_walk > 2.5 and h_walk > 0.4


def test_flat_map_is_infinitely_bad() -> None:
    assert objective(np.zeros((1024, 1024))) == math.inf


def test_build_only_keeps_improvements() -> None:
    history = []
    world = build_map(5, trials=4, progress=lambda _, t, n, best, ok, text: history.append((best, ok)))
    scores = [best for best, _ in history]
    assert scores == sorted(scores, reverse=True)
    assert len(world.gangs) == sum(ok for _, ok in history) >= 1
    assert np.isclose(objective(world.height), scores[-1])

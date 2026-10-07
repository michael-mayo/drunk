"""Tests for drunks' walks and the deposits they leave."""

import numpy as np
import pytest

from app.drunk import Drunk
from app.drunk import walk_drunks

STEPS = 500


def test_same_seed_gives_same_walk_whatever_else_is_walked() -> None:
    """A drunk's walk depends only on its own seed, not on the other drunks or their order."""
    drunks = [Drunk(seed, kappa_max=0.2, home=(float(seed), 0.0)) for seed in range(20)]
    alone = walk_drunks([drunks[7]], STEPS)
    together = walk_drunks(drunks[::-1], STEPS)
    start = (len(drunks) - 1 - 7) * STEPS
    np.testing.assert_array_equal(together.x[start : start + STEPS], alone.x)
    np.testing.assert_array_equal(together.angle[start : start + STEPS], alone.angle)


def test_different_seeds_give_different_walks() -> None:
    """Two seeds don't produce the same path."""
    a = walk_drunks([Drunk(1)], STEPS)
    b = walk_drunks([Drunk(2)], STEPS)
    assert not np.array_equal(a.x, b.x)


def test_steps_deposits_and_decay() -> None:
    """Each step is step_size long from home; deposits have the configured shape and decaying amplitude."""
    drunk = Drunk(5, step_size=0.7, variance=2.0, decay=0.99, initial_amplitude=3.0, home=(4.0, -1.0))
    d = walk_drunks([drunk], STEPS)
    path_x = np.concatenate([[drunk.home[0]], d.x])
    path_y = np.concatenate([[drunk.home[1]], d.y])
    np.testing.assert_allclose(np.hypot(np.diff(path_x), np.diff(path_y)), 0.7)
    np.testing.assert_allclose(d.amplitude, 3.0 * 0.99 ** np.arange(STEPS))
    assert np.all(d.var_major == 2.0)
    assert np.all((d.var_minor > 0) & (d.var_minor <= 2.0))
    assert np.all((d.angle >= 0) & (d.angle < np.pi))


def test_homeward_bias_keeps_drunks_closer_to_home() -> None:
    """Strongly biased drunks stay nearer home than unbiased ones."""
    seeds = range(40)
    free = walk_drunks([Drunk(s, kappa_max=0.0) for s in seeds], STEPS)
    biased = walk_drunks([Drunk(s, kappa_max=2.0) for s in seeds], STEPS)
    assert np.mean(np.hypot(biased.x, biased.y)) < 0.5 * np.mean(np.hypot(free.x, free.y))


@pytest.mark.parametrize("seed", [-1, 2**32])
def test_rejects_seeds_numba_cannot_use(seed: int) -> None:
    """numba's generator takes 32-bit seeds, so anything else would be silently changed."""
    with pytest.raises(ValueError):
        Drunk(seed)

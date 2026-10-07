"""Tests for the layered height field, sampling, sea level, drainage, rivers and the whole pipeline."""

from dataclasses import replace

import numpy as np
import pytest

from app.config import Config
from app.drainage import fill_hollows
from app.layered_drunk import _resample_periodic
from app.pipeline import generate_terrain
from app.pipeline import playable_crop
from app.rivers import RiverParams
from app.rivers import carve_rivers
from app.sampling import poisson_disk_points
from app.sampling import power_log_spacing
from app.sea import sea_level
from tests.conftest import assert_drains


def rough_field(seed: int, n: int = 64) -> np.ndarray:
    """A bumpy wrap-around test surface, full of hollows, scaled to [0, 1]."""
    rng = np.random.default_rng(seed)
    k = np.fft.fftfreq(n)
    spectrum = np.hypot(k[:, None], k[None, :])
    spectrum[0, 0] = 1.0
    z = np.real(np.fft.ifft2(np.fft.fft2(rng.normal(size=(n, n))) * spectrum**-1.5))
    return (z - z.min()) / (z.max() - z.min())


def test_periodic_resample_keeps_constants_and_identity() -> None:
    """Resampling a constant gives the constant; resampling to the same size changes nothing."""
    np.testing.assert_allclose(_resample_periodic(np.full((10, 10), 3.0), 37), 3.0)
    field = rough_field(0, 32)
    np.testing.assert_allclose(_resample_periodic(field, 32), field)


def test_poisson_disk_points_are_spread_across_the_edges() -> None:
    """Exactly n points, inside the square, no two closer than about the chosen spacing (measured across the edges)."""
    side, n = 96.0, 200
    pts = poisson_disk_points(n, side, np.random.default_rng(0), periodic=True)
    assert pts.shape == (n, 2)
    assert np.all((pts >= -side / 2) & (pts < side / 2))
    d = np.abs(pts[:, None, :] - pts[None, :, :])
    d = np.minimum(d, side - d)
    dist = np.hypot(d[..., 0], d[..., 1]) + np.eye(n) * side
    # The spacing poisson_disk_points aims for, sqrt(0.65 A / 1.3 n); retries could shrink it by 10%.
    assert dist.min() >= 0.9 * side * np.sqrt(0.65 / (1.3 * n))


def test_power_log_spacing_ends_and_order() -> None:
    """Values run from start to end, rising, crowded towards start for power > 1."""
    v = power_log_spacing(0.01, 0.4, 20, 4.0)
    assert v[0] == pytest.approx(0.01) and v[-1] == pytest.approx(0.4)
    assert np.all(np.diff(v) > 0)
    assert np.median(v) < np.sqrt(0.01 * 0.4)


def test_sea_level_takes_the_requested_share() -> None:
    """The chosen fraction of the map lies below sea level."""
    field = rough_field(1)
    assert np.mean(field < sea_level(field, 0.3)) == pytest.approx(0.3, abs=1 / field.size)


@pytest.mark.parametrize("periodic", [True, False])
def test_fill_hollows_drains_everything_and_only_raises(periodic: bool) -> None:
    """After filling, every cell drains to the sea (or, on a cut-out grid, the edge), and no cell went down."""
    field = rough_field(2)
    sea = field < sea_level(field, 0.1)
    filled = fill_hollows(field, sea, periodic=periodic)
    assert np.all(filled >= field)
    assert_drains(filled, sea, periodic)


def test_carve_rivers_drains_and_is_reproducible() -> None:
    """Rivers are carved, the result drains to the sea, and the same seed gives the same map."""
    field = rough_field(3, 128)
    level = sea_level(field, 0.15)
    params = RiverParams(sources=30)
    carved, river, n_carved, n_sea = carve_rivers(field, level, params, seed=7)
    again, _, _, _ = carve_rivers(field, level, params, seed=7)
    assert n_carved > 0 and 0 < n_sea <= n_carved
    assert np.any(river > 0)
    assert_drains(carved, carved < level, periodic=True)
    np.testing.assert_array_equal(carved, again)


def test_pipeline_is_reproducible_and_well_formed(small_config: Config) -> None:
    """A whole (small) map: heights in [0, 1], the requested sea, drained rivers, and identical on re-running."""
    a = generate_terrain(small_config, 41)
    b = generate_terrain(small_config, 41)
    c = generate_terrain(small_config, 42)
    np.testing.assert_array_equal(a.field, b.field)
    assert not np.array_equal(a.field, c.field)
    assert a.state == "rivers" and a.rivers_carved > 0
    assert a.field.shape == (96, 96) and a.field.min() >= 0.0 and a.field.max() <= 1.0
    assert a.sea_fraction == pytest.approx(small_config.sea.water_fraction, abs=0.02)
    assert_drains(a.field, a.field < a.sea_level, periodic=True)


def test_pipeline_without_sea_skips_draining(small_config: Config) -> None:
    """With no sea there is nowhere to drain to, so the raw map is returned."""
    config = replace(small_config, sea=replace(small_config.sea, water_fraction=0.0))
    result = generate_terrain(config, 41)
    assert result.state == "no sea" and result.river_area is None


def test_centred_on_rolls_the_chosen_cell_to_the_centre(small_config: Config) -> None:
    """Re-centring moves the chosen cell to the middle and keeps every height and river, just wrapped round."""
    result = generate_terrain(small_config, 41)
    n = result.field.shape[0]
    moved = result.centred_on(10, 70)
    assert moved.field[n // 2, n // 2] == result.field[70, 10]
    np.testing.assert_array_equal(np.roll(moved.field, (70 - n // 2, 10 - n // 2), axis=(0, 1)), result.field)
    np.testing.assert_array_equal(np.roll(moved.river_area, (70 - n // 2, 10 - n // 2), axis=(0, 1)), result.river_area)
    assert moved.sea_level == result.sea_level and moved.rivers_carved == result.rivers_carved


def test_playable_crop_is_the_central_quarter(small_config: Config) -> None:
    """With the CS2 widths, the playable area is the central quarter of the world map's width."""
    field = np.arange(96 * 96, dtype=float).reshape(96, 96)
    crop = playable_crop(small_config, field)
    assert crop.shape == (24, 24)
    np.testing.assert_array_equal(crop, field[36:60, 36:60])


def test_drunks_born_on_parent_paths_take_homes_where_the_parents_deposited() -> None:
    """A share of drunks move to parent deposit centres (wrapped onto the map), favouring strongly biased parents."""
    from app.composite_drunk import CompositeDrunk
    from app.deposits import Deposits
    from app.drunk import Drunk
    from app.pipeline import born_on_paths

    # Two parents of one step each: a wanderer (kappa_max 0.1) that stepped to x = 106 (wraps to 10), y = -20,
    # and a strongly biased one (kappa_max 1.0) that stepped to x = -30, y = 40.
    parent = CompositeDrunk([Drunk(1, kappa_max=0.1), Drunk(2, kappa_max=1.0)])
    parent.deposits = Deposits(np.array([106.0, -30.0]), np.array([-20.0, 40.0]), np.zeros(2), np.ones(2), np.ones(2),
                               np.ones(2))
    parent.num_steps = 1
    drunks = [Drunk(i, kappa_max=0.1 * i, home=(float(i), 5.0)) for i in range(2000)]
    born = born_on_paths(drunks, parent, 0.3, 0.0, 96.0, np.random.default_rng(0))
    moved = [b for d, b in zip(drunks, born) if b.home != d.home]
    assert len(moved) == pytest.approx(600, abs=60)
    assert all(b.home in ((10.0, -20.0), (-30.0, 40.0)) for b in moved)
    assert all(b.seed == d.seed and b.kappa_max == d.kappa_max for d, b in zip(drunks, born))
    # Alike parents share the births; with bias power 2 the strongly biased parent gets ~100x the wanderer's.
    on_wanderer = np.mean([b.home == (10.0, -20.0) for b in moved])
    assert on_wanderer == pytest.approx(0.5, abs=0.07)
    favoured = [b for d, b in zip(drunks, born_on_paths(drunks, parent, 0.3, 2.0, 96.0, np.random.default_rng(0)))
                if b.home != d.home]
    assert np.mean([b.home == (10.0, -20.0) for b in favoured]) < 0.03


def test_born_on_parent_world_generates_and_is_reproducible(small_config: Config) -> None:
    """A world whose fine drunks are born on a coarse layer's paths generates, and the same seed gives the same map."""
    coarse = replace(small_config.layers[3], scale=8.0, drunks=12, weight=4.0, born_on_parent=0.0)
    fine = tuple(replace(layer, born_on_parent=0.8) for layer in small_config.layers)
    config = replace(small_config, layers=fine + (coarse,))
    a, b = generate_terrain(config, 41), generate_terrain(config, 41)
    np.testing.assert_array_equal(a.field, b.field)
    # The scale-4 layer's drunks are born on the scale-8 layer's paths; its reference is the evenly spread layer.
    assert a.terrain.references[3] is not None and a.terrain.references[4] is None


def test_flat_valley_floors_drain_and_lower_the_land(small_config: Config) -> None:
    """With flat valley floors, rivers still drain the map, and carving only ever lowers land compared with V valleys."""
    field = rough_field(4, 128)
    level = sea_level(field, 0.15)
    v_valleys, _, _, _ = carve_rivers(field, level, RiverParams(sources=30), seed=7)
    floors, _, n_carved, _ = carve_rivers(field, level, RiverParams(sources=30, floor_width=0.1), seed=7)
    assert n_carved > 0
    assert_drains(floors, floors < level, periodic=True)
    assert floors.mean() < v_valleys.mean()

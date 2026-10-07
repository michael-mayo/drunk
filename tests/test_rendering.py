"""Tests for drawing terrain maps."""

import io

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.patches import Rectangle

from app import rendering


def test_terrain_map_axes_are_in_km_with_the_playable_area_outlined(monkeypatch: pytest.MonkeyPatch) -> None:
    """The world map spans 0 to its width in km, labelled in km, sits at MAP_RECT, and outlines the playable area."""
    figures = []
    monkeypatch.setattr(plt, "close", figures.append)
    field = np.random.default_rng(0).random((40, 40))
    rivers = np.zeros_like(field)
    rivers[10, 5:30] = np.arange(1, 26)
    buffer = io.BytesIO()
    rendering.save_terrain_map(buffer, field, "test", 0.3, 57.344, rivers=rivers, height_scale_m=1000.0,
                               playable_km=14.336)
    assert buffer.getvalue()[:4] == b"\x89PNG"
    ax = figures[0].axes[0]
    assert ax.get_xlim() == pytest.approx((0.0, 57.344))
    assert ax.get_ylim() == pytest.approx((0.0, 57.344))
    assert ax.get_xlabel() == "x (km)" and ax.get_ylabel() == "y (km)"
    # The map fills MAP_RECT exactly, so the web UI can turn a click into a map position.
    assert tuple(ax.get_position().bounds) == pytest.approx(rendering.MAP_RECT)
    # The playable area is outlined at the centre.
    squares = [p for p in ax.patches if isinstance(p, Rectangle)]
    assert squares and all(p.get_xy() == pytest.approx((21.504, 21.504)) for p in squares)
    assert all(p.get_width() == pytest.approx(14.336) for p in squares)
    plt.close("all")

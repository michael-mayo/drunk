"""Tests for the map container: heights in metres, preview and CS2 heightmaps."""

import io
import json

import numpy as np
from PIL import Image

from app.map import HEIGHTMAP_PX
from app.map import Map
from app.map import View


def bumpy_map() -> Map:
    """A small map with a smooth random height field."""
    world = Map(grid=64)
    world.height = np.random.default_rng(0).random((64, 64)).cumsum(0).cumsum(1)
    return world


def test_metres_put_the_coast_at_the_editor_sea_level() -> None:
    view = View(sea_fraction=0.3, vertical_m=1000.0, sea_level_m=500.0, cx=10, cy=50)
    metres = bumpy_map().metres(view)
    assert np.isclose(np.mean(metres < 500.0), 0.3, atol=0.01)
    assert np.isclose(metres.max() - metres.min(), 1000.0)


def test_metres_roll_the_chosen_cell_to_the_centre() -> None:
    world = bumpy_map()
    metres = world.metres(View(cx=10, cy=50))
    peak = np.unravel_index(np.argmax(world.height), world.height.shape)
    rolled = np.unravel_index(np.argmax(metres), metres.shape)
    assert rolled == ((peak[0] + 32 - 50) % 64, (peak[1] + 32 - 10) % 64)


def test_preview_is_an_rgb_image_one_pixel_per_cell() -> None:
    image = Image.open(io.BytesIO(bumpy_map().preview_png(View())))
    assert image.mode == "RGB" and image.size == (64, 64)


def test_heightmaps_are_16_bit_with_metadata() -> None:
    world = bumpy_map()
    for kind in ("world", "playable"):
        image = Image.open(io.BytesIO(world.heightmap(View(cx=0, cy=0), kind, {"seed": 3})))
        assert image.mode == "I;16" and image.size == (HEIGHTMAP_PX, HEIGHTMAP_PX)
        assert json.loads(image.text["drunk"])["map"] == kind

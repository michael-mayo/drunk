"""Tests for exporting Cities: Skylines II heightmaps."""

import io
import json

import numpy as np
import pytest
from PIL import Image

from app import export
from app.config import Config
from app.pipeline import generate_terrain

# Exported side used in these tests (patched in for speed; the real heightmaps are 4096).
PIXELS = 256


@pytest.fixture(scope="module")
def exported(small_config: Config) -> tuple[export.ExportFile, export.ExportFile]:
    """The two heightmaps of a small map, centred on cell (10, 70), with height 1.0 = 2048 m."""
    patch = pytest.MonkeyPatch()
    patch.setattr(export, "HEIGHTMAP_PIXELS", PIXELS)
    try:
        yield export.export_heightmaps(generate_terrain(small_config, 41), small_config, 40.0, 10, 70, 2048.0)
    finally:
        patch.undo()


def read(file: export.ExportFile) -> tuple[np.ndarray, dict[str, object], str]:
    """The PNG's 16-bit values, its ``drunk`` metadata and its mode."""
    image = Image.open(io.BytesIO(file.png))
    return np.array(image), json.loads(image.text["drunk"]), image.mode


def test_cs2_heightmap_format() -> None:
    """Cities: Skylines II heightmaps are 4096 x 4096, 16-bit, spanning 0-65535."""
    assert export.HEIGHTMAP_PIXELS == 4096 and export.MAX_VALUE == 65535


def test_both_files_are_16_bit_greyscale_and_tagged(exported: tuple[export.ExportFile, export.ExportFile]) -> None:
    """Both PNGs are square 16-bit greyscale, and record the settings that reproduce them in name and metadata."""
    world, playable = exported
    assert world.filename == "drunk_seed41_sea40_x10_y70_peak2048m_world.png"
    assert playable.filename == "drunk_seed41_sea40_x10_y70_peak2048m_playable.png"
    for file, kind in ((world, "world"), (playable, "playable")):
        values, meta, mode = read(file)
        assert mode == "I;16" and values.shape == (PIXELS, PIXELS)
        assert meta["seed"] == 41 and meta["centre_cell"] == [10, 70] and meta["map"] == kind
        assert meta["peak_height_m"] == 2048.0 and meta["sea_percent"] == 40.0


def test_heights_map_to_metres(exported: tuple[export.ExportFile, export.ExportFile]) -> None:
    """Height 1.0 is the peak height (2048 m = half of 4096 m, so about 32768); 0.0 is 0."""
    values, _, _ = read(exported[0])
    assert values.min() == 0
    assert values.max() == pytest.approx(65535 / 2, abs=1)
    np.testing.assert_array_equal(export.to_uint16(np.array([0.0, 0.5, 1.0, 1.2]), 4096.0, 4096.0),
                                  [0, 32768, 65535, 65535])


def test_world_centre_matches_the_playable_area(exported: tuple[export.ExportFile, export.ExportFile]) -> None:
    """The world map's central quarter is the playable-area heightmap at a quarter of the resolution."""
    world = read(exported[0])[0].astype(float)
    playable = read(exported[1])[0].astype(float)
    q = PIXELS // 4
    centre = world[PIXELS // 2 - q // 2 : PIXELS // 2 + q // 2, PIXELS // 2 - q // 2 : PIXELS // 2 + q // 2]
    shrunk = playable.reshape(q, 4, q, 4).mean(axis=(1, 3))
    assert np.corrcoef(centre.ravel(), shrunk.ravel())[0, 1] > 0.99
    assert np.abs(centre - shrunk).mean() < 0.01 * 65535


def test_north_is_up(small_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PNG's top row is the map's northern (last) row, as the map is drawn on screen."""
    monkeypatch.setattr(export, "HEIGHTMAP_PIXELS", 96)
    result = generate_terrain(small_config, 41)
    world, _ = export.export_heightmaps(result, small_config, 40.0, 48, 48, 4096.0)
    values = read(world)[0]
    # Same grid size and no re-centring, so cubic resampling reproduces the cells exactly.
    np.testing.assert_array_equal(values, export.to_uint16(result.field, 4096.0, 4096.0)[::-1])

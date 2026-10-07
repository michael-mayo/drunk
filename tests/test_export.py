"""Tests for exporting Cities: Skylines II heightmaps."""

import io
import json

import numpy as np
import pytest
from PIL import Image

from app import export
from app.config import Config
from app.heights import HeightMapping
from app.pipeline import generate_terrain

# Exported side used in these tests (patched in for speed; the real heightmaps are 4096).
PIXELS = 256


@pytest.fixture(scope="module")
def exported(small_config: Config) -> tuple[export.ExportFile, export.ExportFile]:
    """The two heightmaps of a small map, centred on cell (10, 70), at a 2000 m vertical scale and 511.7 m sea level."""
    patch = pytest.MonkeyPatch()
    patch.setattr(export, "HEIGHTMAP_PIXELS", PIXELS)
    try:
        yield export.export_heightmaps(generate_terrain(small_config, 41), small_config, 40.0, 10, 70, 2000.0, 511.7)
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
    assert world.filename == "drunk_seed41_sea40_x10_y70_scale2000m_sl511.7m_world.png"
    assert playable.filename == "drunk_seed41_sea40_x10_y70_scale2000m_sl511.7m_playable.png"
    for file, kind in ((world, "world"), (playable, "playable")):
        values, meta, mode = read(file)
        assert mode == "I;16" and values.shape == (PIXELS, PIXELS)
        assert meta["seed"] == 41 and meta["centre_cell"] == [10, 70] and meta["map"] == kind
        assert meta["vertical_scale_m"] == 2000.0 and meta["sea_level_m"] == 511.7 and meta["sea_percent"] == 40.0


def test_metres_put_the_coastline_at_the_editor_sea_level() -> None:
    """The model's sea level lands on the editor's; one normalised unit is the vertical scale; below 0 m clips to 0."""
    heights = HeightMapping(2000.0, 511.7, 0.4)
    assert heights.metres(0.4) == pytest.approx(511.7)
    assert heights.metres(1.0) - heights.metres(0.0) == pytest.approx(2000.0)
    assert heights.normalised(heights.metres(0.73)) == pytest.approx(0.73)
    # 0.4 below sea level would be -288.3 m: flattened at 0. 4096 m is the top value.
    np.testing.assert_array_equal(export.to_uint16(np.array([heights.metres(0.0), 0.0, 2048.0, 4096.0, 5000.0]), 4096.0),
                                  [0, 0, 32768, 65535, 65535])


def test_world_centre_matches_the_playable_area(exported: tuple[export.ExportFile, export.ExportFile]) -> None:
    """The world map's central quarter is the playable-area heightmap at a quarter of the resolution."""
    world = read(exported[0])[0].astype(float)
    playable = read(exported[1])[0].astype(float)
    q = PIXELS // 4
    centre = world[PIXELS // 2 - q // 2 : PIXELS // 2 + q // 2, PIXELS // 2 - q // 2 : PIXELS // 2 + q // 2]
    shrunk = playable.reshape(q, 4, q, 4).mean(axis=(1, 3))
    assert np.corrcoef(centre.ravel(), shrunk.ravel())[0, 1] > 0.99
    assert np.abs(centre - shrunk).mean() < 0.01 * 65535


def test_north_is_up_and_heights_follow_the_mapping(small_config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """The PNG's top row is the map's northern (last) row, and its values are the mapping's metres."""
    monkeypatch.setattr(export, "HEIGHTMAP_PIXELS", 96)
    result = generate_terrain(small_config, 41)
    world, _ = export.export_heightmaps(result, small_config, 40.0, 48, 48, 2000.0, 511.7)
    values = read(world)[0]
    # Same grid size and no re-centring, so cubic resampling reproduces the cells exactly.
    expected = export.to_uint16(HeightMapping(2000.0, 511.7, result.sea_level).metres(result.field), 4096.0)
    np.testing.assert_array_equal(values, expected[::-1])
    # Cells within 0.001 of the model's sea level (2 m at this scale) export within 2 m of the editor's sea level.
    coast = np.abs(result.field - result.sea_level) < 1e-3
    assert np.abs(expected[coast] * 4096.0 / 65535 - 511.7).max() < 2.5

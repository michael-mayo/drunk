"""Tests for loading and validating config.yaml."""

from pathlib import Path

import pytest
import yaml

from app.config import DEFAULT_CONFIG_PATH
from app.config import load_config


def write_config(tmp_path: Path, edit: dict[str, object] | None = None, drop: tuple[str, str] | None = None) -> Path:
    """Copy the project's config.yaml into ``tmp_path`` with some settings changed or one removed."""
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    for section, values in (edit or {}).items():
        if isinstance(values, dict):
            raw[section].update(values)
        else:
            raw[section] = values
    if drop:
        del raw[drop[0]][drop[1]]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_project_config_loads() -> None:
    """The shipped config.yaml is valid, and relative paths resolve next to it."""
    config = load_config()
    assert [layer.scale for layer in config.layers] == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]
    assert config.paths.output_dir == DEFAULT_CONFIG_PATH.parent / "output"
    # Cities: Skylines II's world map is 57,344 m across; a heightmap's playable area 14,336 m.
    assert config.cs2.world_width_km == 57.344
    assert config.cs2.playable_width_km == 14.336


def test_missing_setting_is_named(tmp_path: Path) -> None:
    """A missing key fails at startup, naming the setting (including which layer it belongs to)."""
    with pytest.raises(KeyError, match="walk.r0"):
        load_config(write_config(tmp_path, drop=("walk", "r0")))
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    del raw["layers"][2]["weight"]
    with pytest.raises(KeyError, match=r"layers\[2\]\.weight"):
        load_config(write_config(tmp_path, edit={"layers": raw["layers"]}))


@pytest.mark.parametrize(
    "edit",
    [
        {"plot": {"cutoff": 0}},
        {"parallel": {"threads": 0}},
        {"sea": {"water_fraction": 0.99}},
        {"cs2": {"playable_width_km": 0}},
        {"layers": [{"scale": 0.0, "drunks": 10, "kappa_max_start": 0.1, "kappa_max_end": 0.2, "kappa_max_power": 1.0,
                     "weight": 1.0}]},
        {"layers": []},
        {"layers": [0.5, 1.0]},
        {"cs2": {"playable_width_km": 60.0}},
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, edit: dict[str, object]) -> None:
    """Out-of-range settings are refused when the config is loaded."""
    with pytest.raises(ValueError):
        load_config(write_config(tmp_path, edit=edit))

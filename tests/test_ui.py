"""Tests for the web app's build state."""

import json
import math

import pytest

from app import ui
from app.map import Map


def test_state_is_valid_json_before_anything_is_kept(monkeypatch) -> None:
    """Trials before the first kept gang have an infinite objective, which must not reach the page as Infinity."""
    def fake_build(seed, trials, sea_fraction, progress) -> Map:
        world = Map(grid=64)
        progress(world, 1, trials, math.inf, False, "dropped")
        return world

    monkeypatch.setattr(ui, "build_map", fake_build)
    ui._run(1, 2, 0.3)
    state = json.dumps({k: v for k, v in ui.STATE.__dict__.items() if k != "map"})
    json.loads(state, parse_constant=lambda name: (_ for _ in ()).throw(ValueError(name)))
    assert ui.STATE.history == [None] and ui.STATE.error is None


def test_bad_view_numbers_are_rejected() -> None:
    with pytest.raises(ValueError):
        ui._view({"sea": ["lots"]})

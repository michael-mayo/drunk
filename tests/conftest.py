"""Shared fixtures and checks for the test suite."""

from dataclasses import replace

import numpy as np
import pytest

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.drainage import flow_accumulation


@pytest.fixture(scope="session")
def small_config() -> Config:
    """The project's config.yaml shrunk to a map that generates in well under a second."""
    config = load_config(DEFAULT_CONFIG_PATH)
    return replace(
        config,
        composite=replace(config.composite, drunks=30),
        walk=replace(config.walk, num_steps=200),
        plot=replace(config.plot, domain=96.0, grid_points=96),
        rivers=replace(config.rivers, params=replace(config.rivers.params, sources=20)),
    )


def assert_drains(field: np.ndarray, outlets: np.ndarray, periodic: bool) -> None:
    """Assert every cell that isn't an outlet (or, off a periodic map, on the edge) has a strictly lower neighbour.

    Following strictly lower neighbours can't loop, so it must then end at an
    outlet: every such cell drains.
    """
    _, slope = flow_accumulation(field, periodic=periodic)
    stuck = (slope <= 0) & ~outlets
    if not periodic:
        stuck[[0, -1], :] = False
        stuck[:, [0, -1]] = False
    assert not stuck.any(), f"{stuck.sum()} cells have no way down"

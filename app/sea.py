"""Sea level for a height field, set from the fraction of the map that should be water."""

import numpy as np


def sea_level(field: np.ndarray, water_fraction: float) -> float:
    """Height below which ``water_fraction`` of the map lies.

    This is the ``water_fraction`` quantile of the heights, so every map gets
    the same share of sea whatever its height distribution (a fixed height
    such as 0.2 would give very different amounts of sea from map to map).
    The heights themselves are not changed. ``water_fraction = 0`` returns
    the lowest height (no sea).
    """
    if not 0.0 <= water_fraction < 1.0:
        raise ValueError(f"water_fraction must be in [0, 1), got {water_fraction}")
    return float(np.quantile(field, water_fraction))

"""How normalised model heights become metres in Cities: Skylines II.

The model's heights are normalised to [0, 1]. In metres, one unit of
normalised height is ``vertical_scale_m``, and the model's sea level is put
at the map editor's sea level, so the coastline in the game is where it is
on the generated map:

    metres = sea_level_m + (height - model_sea_level) * vertical_scale_m

The same mapping labels the map in the web UI and sets the exported
heightmaps' values, so the two always agree.
"""

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class HeightMapping:
    """Normalised height to metres: ``model_sea_level`` lands on ``sea_level_m``, one unit is ``vertical_scale_m``."""

    # Metres per unit of normalised height: the height from the world's lowest to its highest point.
    vertical_scale_m: float
    # The map editor's sea level, in metres.
    sea_level_m: float
    # The model's sea level, in normalised height.
    model_sea_level: float

    def metres(self, height: float | np.ndarray) -> float | np.ndarray:
        """Normalised ``height`` in metres (can be below 0 m for deep sea floor)."""
        return self.sea_level_m + (height - self.model_sea_level) * self.vertical_scale_m

    def normalised(self, metres: float | np.ndarray) -> float | np.ndarray:
        """The normalised height at ``metres`` (the inverse of ``metres``)."""
        return self.model_sea_level + (metres - self.sea_level_m) / self.vertical_scale_m

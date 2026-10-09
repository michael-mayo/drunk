"""A Cities: Skylines II world map built from gangs of drunks: its height field, preview image and heightmap export.

The map is a square ``WORLD_KM`` across that wraps around at its edges. Its
height is the weighted sum of its gangs' fields on a ``grid`` x ``grid`` grid,
row 0 at the top (north). Heights stay in arbitrary units until shown or
exported: sea level is the height below which ``sea_fraction`` of the map lies,
and ``vertical_m`` metres span the lowest to the highest point. In the game,
the coastline lands on the editor's sea level ``sea_level_m``.

The playable area (``PLAYABLE_KM`` across, the central quarter of the world)
can be put anywhere: the map is rolled so the chosen cell is at the centre.
"""

import io
import json
from dataclasses import asdict
from dataclasses import dataclass

import numpy as np
from PIL import Image
from PIL import PngImagePlugin

from app.drunk import Gang

# Side of the CS2 world map and of its central playable area, in km.
WORLD_KM = 57.344
PLAYABLE_KM = 14.336
# Cells per side of the map's height grid (56 m cells).
GRID = 1024
# CS2 heightmaps: 4096 x 4096 16-bit PNGs spanning 0 to MAX_HEIGHT_M metres.
HEIGHTMAP_PX = 4096
MAX_HEIGHT_M = 4096.0
# The map editor's default sea level, in metres.
EDITOR_SEA_LEVEL_M = 511.7
# Default view settings: share of the map under the sea, and metres from lowest to highest point.
SEA_FRACTION = 0.3
VERTICAL_M = 2500.0
# Preview colours: (position, RGB) stops for sea (by depth, 0 = coast) and land (by height, 0 = coast).
SEA_COLOURS = [(0.0, (118, 178, 214)), (0.3, (66, 128, 186)), (1.0, (22, 52, 104))]
LAND_COLOURS = [(0.0, (120, 168, 96)), (0.15, (160, 182, 112)), (0.35, (196, 178, 128)),
                (0.6, (150, 120, 94)), (0.8, (148, 142, 138)), (1.0, (250, 250, 250))]


def _ramp(t: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    """Colours (``t.shape + (3,)``) interpolated between ``stops`` at positions ``t`` in [0, 1]."""
    positions = [p for p, _ in stops]
    return np.stack([np.interp(t, positions, [c[i] for _, c in stops]) for i in range(3)], axis=-1)


@dataclass(frozen=True)
class View:
    """How a map is shown and exported: sea share, vertical scale, editor sea level and playable centre cell."""

    sea_fraction: float = SEA_FRACTION
    vertical_m: float = VERTICAL_M
    sea_level_m: float = EDITOR_SEA_LEVEL_M
    cx: int = GRID // 2
    cy: int = GRID // 2


class Map:
    """A world map: gangs of drunks with weights, and the height field they sum to."""

    def __init__(self, grid: int = GRID) -> None:
        """An empty (flat) map on a ``grid`` x ``grid`` grid."""
        self.grid = grid
        self.gangs: list[Gang] = []
        self.weights: list[float] = []
        self.height = np.zeros((grid, grid))
        # The sea share and city site (centre cell of the playable area) chosen by the builder.
        self.sea_fraction = SEA_FRACTION
        self.site = (grid // 2, grid // 2)

    def add(self, gang: Gang, weight: float, field: np.ndarray | None = None) -> None:
        """Add ``gang`` with ``weight``; ``field`` is its already rendered field, if at hand."""
        if field is None:
            field = gang.field(WORLD_KM, self.grid)
        self.gangs.append(gang)
        self.weights.append(weight)
        self.height = self.height + weight * field

    def metres(self, view: View) -> np.ndarray:
        """Heights in game metres, rolled so the playable area is centred on cell ``(view.cx, view.cy)``."""
        z = np.roll(self.height, (self.grid // 2 - view.cy, self.grid // 2 - view.cx), axis=(0, 1))
        lo, hi = z.min(), z.max()
        z = (z - lo) / (hi - lo) if hi > lo else np.zeros_like(z)
        return view.sea_level_m + (z - np.quantile(z, view.sea_fraction)) * view.vertical_m

    def preview_png(self, view: View) -> bytes:
        """A hill-shaded colour picture of the whole world, one pixel per cell."""
        m = self.metres(view)
        sea = m < view.sea_level_m
        depth = (view.sea_level_m - m) / max(view.sea_level_m - m.min(), 1e-9)
        rise = (m - view.sea_level_m) / max(m.max() - view.sea_level_m, 1e-9)
        rgb = np.where(sea[..., None], _ramp(depth, SEA_COLOURS), _ramp(rise, LAND_COLOURS))
        # Light from the north-west, 45 degrees up; the sea stays flat.
        cell_m = WORLD_KM * 1000.0 / self.grid
        gy, gx = np.gradient(np.maximum(m, view.sea_level_m), cell_m)
        shade = (gx + gy + np.sqrt(2.0)) / (2.0 * np.sqrt(1.0 + gx**2 + gy**2))
        rgb *= np.clip(0.45 + 0.75 * shade, 0.0, 1.3)[..., None]
        buffer = io.BytesIO()
        Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8)).save(buffer, format="PNG", compress_level=1)
        return buffer.getvalue()

    def heightmap(self, view: View, kind: str, metadata: dict[str, object]) -> bytes:
        """The CS2 ``"world"`` or ``"playable"`` heightmap as a 16-bit PNG, carrying ``metadata`` and ``view``.

        Both are ``HEIGHTMAP_PX`` square, cubic-resampled from the same rolled
        grid; the playable one is its central quarter. Heights outside
        0 to ``MAX_HEIGHT_M`` are clipped.
        """
        corner = {"world": 0.0, "playable": self.grid * (1.0 - PLAYABLE_KM / WORLD_KM) / 2.0}[kind]
        image = Image.fromarray(self.metres(view).astype(np.float32), mode="F")
        box = (corner, corner, self.grid - corner, self.grid - corner)
        metres = np.asarray(image.resize((HEIGHTMAP_PX, HEIGHTMAP_PX), Image.Resampling.BICUBIC, box=box))
        values = np.round(np.clip(metres / MAX_HEIGHT_M, 0.0, 1.0) * 65535).astype(np.uint16)
        info = PngImagePlugin.PngInfo()
        info.add_text("drunk", json.dumps({**metadata, **asdict(view), "map": kind}))
        buffer = io.BytesIO()
        Image.fromarray(values).save(buffer, format="PNG", pnginfo=info, compress_level=3)
        return buffer.getvalue()

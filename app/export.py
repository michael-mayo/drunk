"""Export a generated map as the two Cities: Skylines II heightmaps: the world map and the playable area.

Both are 4096 x 4096, 16-bit greyscale PNGs. The playable-area heightmap
covers ``cs2.playable_width_km`` (14.336 km, 3.5 m per pixel); the world map
covers ``cs2.world_width_km`` (57.344 km, 14 m per pixel) with the playable
area as its central 1024 x 1024 pixels, as the game requires. Pixel values
0-65535 span 0 m to ``cs2.max_height_m`` (4096 m at the editor's default
height scale); normalised height 1.0 is exported as the chosen peak height.

The model's grid (1600 x 1600 over the world) is resampled with cubic
interpolation. Its finest deposits are about two cells wide, so the grid
already holds all the terrain's detail and cubic resampling adds smooth
in-between values rather than the kinks bilinear resampling would put in
every slope. Both files are drawn from the same re-centred world, so they
match exactly.

Each PNG carries the settings that reproduce it (seed, sea share, centre
cell, peak height, sea level) in its file name and in a ``drunk`` text chunk.
"""

import io
import json
import math
from dataclasses import dataclass

import numba
import numpy as np
from PIL import Image
from PIL import PngImagePlugin

from app.config import Config
from app.pipeline import TerrainResult

# Side of both CS2 heightmaps, in pixels.
HEIGHTMAP_PIXELS = 4096
# Largest 16-bit value: the editor's maximum height.
MAX_VALUE = 65535
# zlib level for the PNGs: 3 makes files as small as the default 6 for terrain, about 3.5x faster.
PNG_COMPRESS_LEVEL = 3


@dataclass(frozen=True)
class ExportFile:
    """One exported heightmap: its suggested file name and PNG bytes."""

    filename: str
    png: bytes


@numba.njit(cache=True)
def _cubic_weights(t: float) -> tuple[float, float, float, float]:
    """Keys cubic-convolution weights (a = -0.5) for the four samples around fractional position ``t``."""
    a = -0.5
    w0 = ((a * (t + 1.0) - 5.0 * a) * (t + 1.0) + 8.0 * a) * (t + 1.0) - 4.0 * a
    w1 = ((a + 2.0) * t - (a + 3.0)) * t * t + 1.0
    w2 = ((a + 2.0) * (1.0 - t) - (a + 3.0)) * (1.0 - t) * (1.0 - t) + 1.0
    w3 = 1.0 - w0 - w1 - w2
    return w0, w1, w2, w3


@numba.njit(parallel=True, cache=True)
def _resample_cubic(field: np.ndarray, x0: float, y0: float, size: float, n: int) -> np.ndarray:
    """Cubic resample of the square window ``[x0, x0 + size)^2`` (in cells) of wrap-around ``field`` onto ``n`` x ``n`` pixels.

    Cell ``j`` has its value at its centre, ``j + 0.5``; pixel ``i`` is sampled
    at its centre too, so the window maps onto the pixels edge to edge.
    """
    n_y, n_x = field.shape
    out = np.empty((n, n))
    step = size / n
    for i in numba.prange(n):
        v = y0 + (i + 0.5) * step - 0.5
        iy = int(math.floor(v))
        wy = _cubic_weights(v - iy)
        for j in range(n):
            u = x0 + (j + 0.5) * step - 0.5
            ix = int(math.floor(u))
            wx = _cubic_weights(u - ix)
            total = 0.0
            for a in range(4):
                row = (iy - 1 + a) % n_y
                acc = 0.0
                for b in range(4):
                    acc += wx[b] * field[row, (ix - 1 + b) % n_x]
                total += wy[a] * acc
            out[i, j] = total
    return out


def to_uint16(heights: np.ndarray, peak_m: float, max_height_m: float) -> np.ndarray:
    """Normalised heights (1.0 = ``peak_m`` metres) as 16-bit values spanning 0 m to ``max_height_m``."""
    metres = np.clip(heights, 0.0, 1.0) * peak_m
    return np.round(metres / max_height_m * MAX_VALUE).astype(np.uint16)


def _png(values: np.ndarray, metadata: dict[str, object]) -> bytes:
    """``values`` (row 0 = south) as a 16-bit greyscale PNG with north at the top, carrying ``metadata``."""
    info = PngImagePlugin.PngInfo()
    info.add_text("drunk", json.dumps(metadata))
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(values[::-1])).save(buffer, format="PNG", pnginfo=info,
                                                             compress_level=PNG_COMPRESS_LEVEL)
    return buffer.getvalue()


def export_heightmaps(
    result: TerrainResult,
    config: Config,
    sea_percent: float,
    cx: int,
    cy: int,
    peak_m: float,
) -> tuple[ExportFile, ExportFile]:
    """The world map and playable-area heightmaps of ``result`` with the playable area centred on cell ``(cx, cy)``.

    ``sea_percent`` is the sea share the map was generated with (only
    recorded), and ``peak_m`` the height in metres of normalised height 1.0.
    Returns ``(world, playable)``.
    """
    if not 0 < peak_m <= config.cs2.max_height_m:
        raise ValueError(f"peak height must be in (0, {config.cs2.max_height_m:g}] m, got {peak_m}")
    centred = result.centred_on(cx, cy)
    n = centred.field.shape[0]
    playable_cells = n * config.cs2.playable_width_km / config.cs2.world_width_km
    world = _resample_cubic(centred.field, 0.0, 0.0, float(n), HEIGHTMAP_PIXELS)
    corner = (n - playable_cells) / 2.0
    playable = _resample_cubic(centred.field, corner, corner, playable_cells, HEIGHTMAP_PIXELS)

    metadata = {
        "seed": result.seed,
        "sea_percent": sea_percent,
        "centre_cell": [cx, cy],
        "grid_points": n,
        "peak_height_m": peak_m,
        "sea_level_m": round(result.sea_level * peak_m, 2),
        "max_height_m": config.cs2.max_height_m,
    }
    stem = f"drunk_seed{result.seed}_sea{sea_percent:g}_x{cx}_y{cy}_peak{peak_m:g}m"
    files = []
    for kind, heights, width_km in (("world", world, config.cs2.world_width_km),
                                    ("playable", playable, config.cs2.playable_width_km)):
        values = to_uint16(heights, peak_m, config.cs2.max_height_m)
        files.append(ExportFile(f"{stem}_{kind}.png", _png(values, {**metadata, "map": kind, "width_km": width_km})))
    return files[0], files[1]

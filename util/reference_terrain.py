"""Cut square reference crops from Copernicus GLO-30 elevation tiles.

Tiles are the 1 x 1 degree Cloud-Optimised GeoTIFFs published at
``https://copernicus-dem-30m.s3.amazonaws.com/`` (free, no login), named like
``Copernicus_DSM_COG_10_N46_00_E008_00_DEM.tif``. Rows are spaced 1 arcsecond
(~30.9 m) apart; columns 1 arcsecond apart below 50 degrees latitude and 1.5
arcseconds from 50 to 60 degrees, so the east-west spacing depends on
latitude. Crops are cut to a true square in metres and resampled to a square
pixel grid.
"""

import math
import re
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image

# Metres per arcsecond of latitude.
_M_PER_ARCSEC = 30.87
_TILE_PATTERN = re.compile(r"_([NS])(\d+)_00_([EW])(\d+)_00_DEM")


def tile_latitude(path: Path) -> float:
    """Latitude (degrees) of the centre of the tile named by ``path``."""
    match = _TILE_PATTERN.search(path.name)
    if not match:
        raise ValueError(f"not a Copernicus tile name: {path.name}")
    lat = int(match.group(2)) * (1 if match.group(1) == "N" else -1)
    return lat + 0.5


def square_crops(
    path: Path,
    side_m: float,
    pixels: int,
    max_crops: int,
    rng: np.random.Generator,
    max_sea_fraction: float = 0.02,
) -> list[np.ndarray]:
    """Cut up to ``max_crops`` non-overlapping ``side_m`` x ``side_m`` squares from a tile.

    Crops are chosen from a grid of candidate cells in random order; a crop is
    skipped if more than ``max_sea_fraction`` of it is at or below 1 m (sea),
    since flat sea would distort the statistics. Each crop is resampled to
    ``pixels`` x ``pixels`` (bilinear) and returned in metres.
    """
    dem = tifffile.imread(path).astype(np.float32)
    rows, cols = dem.shape
    lat = tile_latitude(path)
    dy = _M_PER_ARCSEC
    dx = _M_PER_ARCSEC * (3600 / cols) * math.cos(math.radians(lat))
    crop_rows = round(side_m / dy)
    crop_cols = round(side_m / dx)
    cells = [(r, c) for r in range(rows // crop_rows) for c in range(cols // crop_cols)]
    crops = []
    for i in rng.permutation(len(cells)):
        r, c = cells[i]
        crop = dem[r * crop_rows : (r + 1) * crop_rows, c * crop_cols : (c + 1) * crop_cols]
        if np.mean(crop <= 1.0) > max_sea_fraction:
            continue
        resized = Image.fromarray(crop, mode="F").resize((pixels, pixels), Image.Resampling.BILINEAR)
        crops.append(np.asarray(resized, dtype=float))
        if len(crops) == max_crops:
            break
    return crops

"""Cut a 57.344 km square around every reference site from the FABDEM tiles and measure it.

Each square is centred on its site, north at the top, and sampled onto the
map's 1024 x 1024 grid (56 m cells): the tiles are bilinearly sampled at
twice that resolution in a local east-north projection, then averaged in
2 x 2 blocks. Water is sea (0 m, or a missing tile) and lakes (cells whose
eight neighbours all have exactly the same height, as FABDEM flattens water).

Writes ``data/windows/<site>.npz`` (height in decimetres, water),
``data/reference.npz`` (every site's statistics, which ``app/build_map.py``
loads as REFERENCE), ``data/reference.txt`` (the same, readable) and
``data/sites.png`` (a contact sheet of all sites); all are kept in git (via LFS). Run from the project root: ``python fabdem/reference.py``.
Squares already in ``data/windows/`` are reused, so the raw tiles
(``fabdem/download.py``) are only needed for new sites.
"""

import math
import sys
from pathlib import Path

import numpy as np
import tifffile
from PIL import Image
from PIL import ImageDraw

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
from download import KM_PER_DEGREE
from download import TILES
from download import corner
from download import tiles_for
from locations import CITIES
from locations import WILD

from app.build_map import STAT_NAMES
from app.build_map import stats
from app.map import GRID
from app.map import PLAYABLE_KM
from app.map import WORLD_KM

DATA = ROOT / "data"
CELLS_PER_DEGREE = 3600
SUPERSAMPLE = 2


def tile(lat: int, lon: int) -> np.ndarray:
    """The 1-degree tile with south-west corner ``(lat, lon)`` in metres (zeros for an all-sea tile)."""
    path = TILES / f"{corner(lat, lon)}_FABDEM_V1-2.tif"
    if not path.exists():
        return np.zeros((CELLS_PER_DEGREE, CELLS_PER_DEGREE), dtype=np.float32)
    z = tifffile.imread(path).astype(np.float32)
    z[z < -1000] = 0.0  # nodata
    return z


def flat(z: np.ndarray) -> np.ndarray:
    """Cells whose eight neighbours all have the same height as them (FABDEM's flattened water)."""
    out = np.zeros(z.shape, dtype=bool)
    inner = np.ones((z.shape[0] - 2, z.shape[1] - 2), dtype=bool)
    centre = z[1:-1, 1:-1]
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            inner &= z[1 + dy : z.shape[0] - 1 + dy, 1 + dx : z.shape[1] - 1 + dx] == centre
    out[1:-1, 1:-1] = inner
    return out


def window(lat0: float, lon0: float) -> tuple[np.ndarray, np.ndarray]:
    """``(height in metres, water)`` of the square centred on ``(lat0, lon0)``, each ``GRID`` x ``GRID``, north up."""
    corners = tiles_for(lat0, lon0)
    lats = range(min(a for a, _ in corners), max(a for a, _ in corners) + 1)
    lons = range(min(b for _, b in corners), max(b for _, b in corners) + 1)
    mosaic = np.block([[tile(a, b) for b in lons] for a in reversed(lats)])
    water = (mosaic == 0.0) | flat(mosaic)
    top, left = lats[-1] + 1, lons[0]

    m = GRID * SUPERSAMPLE
    offset_km = ((np.arange(m) + 0.5) / m - 0.5) * WORLD_KM
    rows = (top - (lat0 - offset_km / KM_PER_DEGREE)) * CELLS_PER_DEGREE - 0.5
    cols = (lon0 + offset_km / (KM_PER_DEGREE * math.cos(math.radians(lat0))) - left) * CELLS_PER_DEGREE - 0.5
    r0, c0 = np.floor(rows).astype(int), np.floor(cols).astype(int)
    wr, wc = (rows - r0)[:, None], (cols - c0)[None, :]
    z = ((mosaic[np.ix_(r0, c0)] * (1 - wc) + mosaic[np.ix_(r0, c0 + 1)] * wc) * (1 - wr)
         + (mosaic[np.ix_(r0 + 1, c0)] * (1 - wc) + mosaic[np.ix_(r0 + 1, c0 + 1)] * wc) * wr)
    wet = water[np.ix_(np.round(rows).astype(int), np.round(cols).astype(int))]

    def blocks(a: np.ndarray) -> np.ndarray:
        return a.reshape(GRID, SUPERSAMPLE, GRID, SUPERSAMPLE).mean(axis=(1, 3))

    return blocks(z), blocks(wet.astype(float)) > 0.5


def thumbnail(z: np.ndarray, water: np.ndarray, label: str, px: int = 256) -> Image.Image:
    """A small hill-shaded picture of a square, water in blue, with its name."""
    gy, gx = np.gradient(z, WORLD_KM * 1000 / z.shape[0])
    shade = np.clip((gx + gy + math.sqrt(2)) / (2 * np.sqrt(1 + gx**2 + gy**2)), 0, 1)
    grey = (60 + 180 * shade)[..., None] * np.ones(3)
    rgb = np.where(water[..., None], np.array([70, 120, 185]), grey).astype(np.uint8)
    image = Image.fromarray(rgb).resize((px, px), Image.Resampling.BILINEAR)
    ImageDraw.Draw(image).text((4, 4), label, fill=(255, 255, 0))
    return image


def main() -> None:
    """Cut, measure and summarise every site."""
    (DATA / "windows").mkdir(parents=True, exist_ok=True)
    sites = [(name, p, "city") for name, p in CITIES.items()] + [(name, p, "wild") for name, p in WILD.items()]
    half = round(GRID * PLAYABLE_KM / WORLD_KM) // 2
    world_rows, city_rows, world_names, city_names, lines, thumbs = [], [], [], [], [], []
    for name, (lat, lon), kind in sites:
        path = DATA / "windows" / f"{name.lower().replace(' ', '_')}.npz"
        if path.exists():
            saved = np.load(path)
            z, water = saved["height_dm"] / 10.0, saved["water"]
        else:
            z, water = window(lat, lon)
            # Whole decimetres compress far better than floats and are plenty for 56 m cells.
            np.savez_compressed(path, height_dm=np.round(z * 10).astype(np.int32), water=water)
            z = np.round(z * 10) / 10.0
        world = stats(z, ~water)
        world_rows.append(world)
        world_names.append(name)
        row = f"{kind:4s}  {name:22s} world " + " ".join(f"{v:6.2f}" for v in world)
        if kind == "city":
            c = slice(GRID // 2 - half, GRID // 2 + half)
            centre = stats(z[c, c], ~water[c, c])
            city_rows.append(centre)
            city_names.append(name)
            row += "   centre " + " ".join(f"{v:6.2f}" for v in centre)
        lines.append(row)
        thumbs.append(thumbnail(z, water, name))
        print(row, flush=True)

    sheet = Image.new("RGB", (256 * 10, 256 * math.ceil(len(thumbs) / 10)))
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % 10) * 256, (i // 10) * 256))
    sheet.save(DATA / "sites.png")

    np.savez(DATA / "reference.npz", world=np.array(world_rows), world_names=np.array(world_names),
             city=np.array(city_rows), city_names=np.array(city_names))
    lines += ["", f"{len(world_rows)} world squares, {len(city_rows)} city centres; columns: {', '.join(STAT_NAMES)}"]
    (DATA / "reference.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()

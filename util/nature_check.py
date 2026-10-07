"""Check that generated world maps still look like real terrain, window by window, and report their seas and lakes.

Generates one finished world map (sea level set, rivers carved) per seed from
the app config. Each world is cut into playable-sized windows (16 for the
default 4 x 4), and every window is measured exactly like the real-terrain
crops it is compared with (14.336 km squares of Copernicus GLO-30 tiles, see
``util/reference_terrain.py``):

- spectral slope beta, roughness H, hypsometric integral HI and skewness
  (``util.terrain_stats.terrain_stats``);
- channel concavity theta, with the window's edges and its sea cells as
  outlets (``util.terrain_stats.drainage_stats``).

The model's mean of each statistic is compared with the real crops' mean and
spread; the distance is the RMS of the z-scores (0 = matches real terrain).

It also describes the world's large-scale structure at its sea level: seas
(below-sea regions covering at least 1% of the world, connected across the
wrap-around edges), lakes (smaller below-sea regions of at least 20 cells),
and how much the sea share varies between windows.

The summary is printed and written to ``output/experiments/nature_check.txt``.
"""

import argparse
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numba
import numpy as np

# Make the project root importable when run as a script.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import load_config
from app.pipeline import generate_terrain
from util.reference_terrain import square_crops
from util.terrain_stats import drainage_stats
from util.terrain_stats import terrain_stats

# Project root; the reference tiles and the output folder are relative to it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Statistic names, in output order.
STAT_NAMES = ["beta", "H", "HI", "skew", "concavity"]
# Below-sea regions at least this share of the world are seas; smaller ones are lakes.
MIN_SEA_SHARE = 0.01
# Below-sea regions smaller than this many cells are not counted as lakes.
MIN_LAKE_CELLS = 20


def window_stats(window: np.ndarray, sea_level: float) -> list[float]:
    """The five statistics of one window, measured like a real-terrain crop (edges and sea drain out)."""
    stats = list(asdict(terrain_stats(window)).values())
    return stats + [drainage_stats(window, window < sea_level, periodic=False).concavity]


def nature_stats(dem_dir: Path, pixels: int, crops_per_tile: int, side_m: float) -> np.ndarray:
    """The five statistics of square crops of every Copernicus tile in ``dem_dir``, shape (crops, 5)."""
    rng = np.random.default_rng(0)
    rows = []
    for tile in sorted(dem_dir.glob("Copernicus_DSM_COG_10_*_DEM.tif")):
        for crop in square_crops(tile, side_m, pixels, crops_per_tile, rng):
            stats = list(asdict(terrain_stats(crop)).values())
            rows.append(stats + [drainage_stats(crop, np.zeros(crop.shape, dtype=bool), periodic=False).concavity])
    if not rows:
        raise FileNotFoundError(f"no Copernicus tiles found in {dem_dir} (see the README's Setup section)")
    return np.array(rows)


@numba.njit(cache=True)
def _region_sizes(mask: np.ndarray) -> np.ndarray:
    """Sizes of the connected regions (8 neighbours, wrapping round the edges) of ``mask``."""
    n_y, n_x = mask.shape
    seen = np.zeros((n_y, n_x), dtype=np.bool_)
    stack = np.empty(n_y * n_x, dtype=np.int64)
    sizes = []
    for y in range(n_y):
        for x in range(n_x):
            if not mask[y, x] or seen[y, x]:
                continue
            seen[y, x] = True
            stack[0] = y * n_x + x
            top = 1
            size = 0
            while top > 0:
                top -= 1
                cy = stack[top] // n_x
                cx = stack[top] % n_x
                size += 1
                for dy in range(-1, 2):
                    for dx in range(-1, 2):
                        ny = (cy + dy) % n_y
                        nx = (cx + dx) % n_x
                        if mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack[top] = ny * n_x + nx
                            top += 1
            sizes.append(size)
    return np.array(sizes, dtype=np.int64)


def water_bodies(field: np.ndarray, sea_level: float) -> tuple[int, float, int, float]:
    """``(seas, sea share, lakes, lake share)`` of the world below ``sea_level``."""
    sizes = _region_sizes(field < sea_level)
    sea = sizes >= MIN_SEA_SHARE * field.size
    lake = ~sea & (sizes >= MIN_LAKE_CELLS)
    return int(sea.sum()), float(sizes[sea].sum() / field.size), int(lake.sum()), float(sizes[lake].sum() / field.size)


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Compare generated world maps with real terrain, window by window.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="App config file (default: config.yaml).")
    parser.add_argument("--seeds", type=str, default="41,42,43,44,45", help="Comma-separated seeds (default: 41,42,43,44,45).")
    parser.add_argument("--dem-dir", type=Path, default=PROJECT_ROOT / "data" / "dem", help="Folder of Copernicus tiles.")
    parser.add_argument("--crops-per-tile", type=int, default=8, help="Real-terrain crops per tile (default: 8).")
    return parser.parse_args()


def main() -> None:
    """Measure the worlds and the real crops, and print and save the comparison."""
    args = _parse_args()
    config = load_config(args.config)
    seeds = [int(s) for s in args.seeds.split(",")]
    window = round(config.plot.grid_points * config.cs2.playable_width_km / config.cs2.world_width_km)
    nature = nature_stats(args.dem_dir, window, args.crops_per_tile, config.cs2.playable_width_km * 1000.0)
    nature_mean, nature_sd = nature.mean(axis=0), nature.std(axis=0)

    started = time.time()
    rows, structure = [], []
    for seed in seeds:
        result = generate_terrain(config, seed)
        field, level = result.field, result.sea_level
        k = field.shape[0] // window
        windows = [field[i * window : (i + 1) * window, j * window : (j + 1) * window] for i in range(k) for j in range(k)]
        rows += [window_stats(w, level) for w in windows]
        shares = [float(np.mean(w < level)) for w in windows]
        structure.append([*water_bodies(field, level), float(np.std(shares))])
    model = np.array(rows)
    model_mean, model_sd = np.nanmean(model, axis=0), np.nanstd(model, axis=0)
    distance = float(np.sqrt(np.mean(((model_mean - nature_mean) / nature_sd) ** 2)))
    seas, sea_share, lakes, lake_share, window_sd = np.mean(structure, axis=0)

    lines = [
        f"{len(seeds)} worlds ({', '.join(map(str, seeds))}), {len(rows)} windows of {window} x {window} cells, "
        f"{len(nature)} real crops; {time.time() - started:.0f} s",
        "",
        f"{'':22s}" + "".join(f"{name:>16s}" for name in STAT_NAMES),
        f"{'real terrain':22s}" + "".join(f"{m:9.3f} ± {s:<4.2f}" for m, s in zip(nature_mean, nature_sd)),
        f"{'generated windows':22s}" + "".join(f"{m:9.3f} ± {s:<4.2f}" for m, s in zip(model_mean, model_sd)),
        f"{'z-score of the mean':22s}" + "".join(f"{z:16.2f}" for z in (model_mean - nature_mean) / nature_sd),
        "",
        f"distance from real terrain (RMS z-score): {distance:.2f}",
        f"per world, at {config.sea.water_fraction:.0%} below sea level: {seas:.1f} seas covering {sea_share:.0%}, "
        f"{lakes:.0f} lakes covering {lake_share:.1%}, sea share varying by {window_sd:.0%} (sd) between windows",
    ]
    summary = "\n".join(lines)
    print(summary)
    out = config.paths.output_dir / "experiments"
    out.mkdir(parents=True, exist_ok=True)
    (out / "nature_check.txt").write_text(summary + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

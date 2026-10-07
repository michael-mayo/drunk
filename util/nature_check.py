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

Second, it compares windows with real cities set between sea and mountains
(``CITIES``: windows centred on each city, from Copernicus tiles in
``data/dem_cities``). City terrain has flat lowlands and steep highlands, so
it is measured in metres, at the app's vertical scale and editor sea level:
the share of land under 3 and 6 degrees, the median slope of the lowest and
highest quarter of the land, and the skewness of land heights. It reports
the median over all windows with at most 60% sea, the most city-like window
of each world (smallest RMS z-score over those measures plus spectral slope,
roughness and concavity against the city windows), and the vertical scale
at which the windows' highland slopes match the cities': the calibration for
``ui.vertical_scale_m``.

It also describes the world's large-scale structure at its sea level: seas
(below-sea regions covering at least 1% of the world, connected across the
wrap-around edges), lakes (smaller below-sea regions of at least 20 cells),
and how much the sea share varies between windows.

The summary is printed and written to ``output/experiments/nature_check.txt``.
"""

import argparse
import math
import sys
import time
from dataclasses import asdict
from pathlib import Path

import numba
import numpy as np
import tifffile
from PIL import Image

# Make the project root importable when run as a script.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import load_config
from app.heights import HeightMapping
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


# Cities between sea and mountains: name -> (latitude, longitude, Copernicus tile). Each window is centred on the city.
CITIES = {
    "Vancouver": (49.283, -123.121, "N49_00_W124_00"),
    "Seattle": (47.606, -122.332, "N47_00_W123_00"),
    "Salt Lake City": (40.761, -111.891, "N40_00_W112_00"),
    "Innsbruck": (47.269, 11.404, "N47_00_E011_00"),
    "Rio de Janeiro": (-22.906, -43.173, "S23_00_W044_00"),
    "Cape Town": (-33.925, 18.424, "S34_00_E018_00"),
    "Wellington": (-41.287, 174.776, "S42_00_E174_00"),
}
# City measures, in output order.
CITY_NAMES = ["land<3°", "land<6°", "low slope°", "mid slope°", "high slope°", "skew"]
# Index of the highland slope in the city measures (used to calibrate the vertical scale).
HIGH_SLOPE = 4
# Windows with more sea than this are not compared with cities.
MAX_CITY_SEA = 0.6
# Metres per degree of latitude.
M_PER_DEGREE = 111_320.0


def city_measures(metres: np.ndarray, sea_level_m: float, side_m: float) -> np.ndarray | None:
    """``[land < 3 deg, land < 6 deg, median slope of the lowest 25%, middle 50% and highest 25% of land, land height skewness]``.

    ``metres`` is a square window ``side_m`` across; land is above
    ``sea_level_m``. Returns None if under 5% of the window is land.
    """
    gy, gx = np.gradient(metres, side_m / metres.shape[0])
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))
    land = metres > sea_level_m
    if land.mean() < 0.05:
        return None
    s, e = slope[land], metres[land]
    low, high = np.quantile(e, [0.25, 0.75])
    skew = float(np.mean((e - e.mean()) ** 3) / e.std() ** 3)
    middle = (e > low) & (e < high)
    return np.array([np.mean(s < 3), np.mean(s < 6), np.median(s[e <= low]), np.median(s[middle]),
                     np.median(s[e >= high]), skew])


def city_window(dem_dir: Path, name: str, side_m: float, pixels: int) -> np.ndarray:
    """The ``side_m`` square around city ``name`` (metres), resampled to ``pixels`` x ``pixels``, north at the top."""
    lat, lon, tile = CITIES[name]
    dem = tifffile.imread(dem_dir / f"Copernicus_DSM_COG_10_{tile}_DEM.tif").astype(np.float32)
    lat0 = int(tile[1:3]) * (1 if tile[0] == "N" else -1)
    lon0 = int(tile[8:11]) * (1 if tile[7] == "E" else -1)
    rows, cols = dem.shape
    row, col = (lat0 + 1 - lat) * rows, (lon - lon0) * cols
    half_rows = side_m / 2 / M_PER_DEGREE * rows
    half_cols = side_m / 2 / (M_PER_DEGREE * math.cos(math.radians(lat))) * cols
    crop = dem[int(row - half_rows) : int(row + half_rows), int(col - half_cols) : int(col + half_cols)]
    resized = Image.fromarray(crop, mode="F").resize((pixels, pixels), Image.Resampling.BILINEAR)
    return np.asarray(resized, dtype=float)[::-1]


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
    parser.add_argument("--at-calibrated-scale", action="store_true",
                        help="Measure the city comparison at the calibrated vertical scale instead of ui.vertical_scale_m.")
    parser.add_argument("--city-dem-dir", type=Path, default=PROJECT_ROOT / "data" / "dem_cities",
                        help="Folder of Copernicus tiles around the cities (default: data/dem_cities).")
    return parser.parse_args()


def _windows(field: np.ndarray, size: int) -> list[tuple[slice, slice]]:
    """The playable-sized windows tiling ``field``."""
    k = field.shape[0] // size
    return [(slice(i * size, (i + 1) * size), slice(j * size, (j + 1) * size)) for i in range(k) for j in range(k)]


def calibrate_vertical_scale(worlds: list[tuple[np.ndarray, float]], size: int, side_m: float, target: float,
                             sea_level_m: float) -> float:
    """The vertical scale at which the windows' median highland slope equals ``target`` degrees (bisection)."""
    def highland(scale_m: float) -> float:
        values = []
        for field, level in worlds:
            metres = HeightMapping(scale_m, sea_level_m, level).metres(field)
            for rows, cols in _windows(field, size):
                if np.mean(field[rows, cols] < level) <= MAX_CITY_SEA:
                    m = city_measures(metres[rows, cols], sea_level_m, side_m)
                    if m is not None:
                        values.append(m[HIGH_SLOPE])
        return float(np.median(values))

    lo, hi = 100.0, 50_000.0
    for _ in range(40):
        mid = math.sqrt(lo * hi)
        lo, hi = (mid, hi) if highland(mid) < target else (lo, mid)
    return math.sqrt(lo * hi)


def main() -> None:
    """Measure the worlds and the real crops, and print and save the comparison."""
    args = _parse_args()
    config = load_config(args.config)
    seeds = [int(s) for s in args.seeds.split(",")]
    window = round(config.plot.grid_points * config.cs2.playable_width_km / config.cs2.world_width_km)
    nature = nature_stats(args.dem_dir, window, args.crops_per_tile, config.cs2.playable_width_km * 1000.0)
    nature_mean, nature_sd = nature.mean(axis=0), nature.std(axis=0)

    side_m = config.cs2.playable_width_km * 1000.0
    city_windows = [city_window(args.city_dem_dir, name, side_m, window) for name in CITIES]
    city = np.array([city_measures(w, 1.0, side_m) for w in city_windows])
    city_tex = np.array([window_stats(w, 1.0) for w in city_windows])
    city_mean, city_sd = city.mean(axis=0), city.std(axis=0)
    city_tex_mean, city_tex_sd = np.nanmean(city_tex, axis=0), np.nanstd(city_tex, axis=0)

    started = time.time()
    rows, structure, worlds, window_tex = [], [], [], []
    sea_level_m = config.cs2.editor_sea_level_m
    for seed in seeds:
        result = generate_terrain(config, seed)
        field, level = result.field, result.sea_level
        worlds.append((field, level))
        stats = [window_stats(field[r, c], level) for r, c in _windows(field, window)]
        rows += stats
        window_tex.append(stats)
        shares = [float(np.mean(field[r, c] < level)) for r, c in _windows(field, window)]
        structure.append([*water_bodies(field, level), float(np.std(shares))])
    calibrated = calibrate_vertical_scale(worlds, window, side_m, city_mean[HIGH_SLOPE], sea_level_m)
    scale_m = calibrated if args.at_calibrated_scale else config.ui.vertical_scale_m

    # City comparison, in metres at the chosen vertical scale.
    city_rows, best = [], []
    for (field, level), stats in zip(worlds, window_tex):
        metres = HeightMapping(scale_m, sea_level_m, level).metres(field)
        candidates = []
        for (r, c), window_stat in zip(_windows(field, window), stats):
            measures = city_measures(metres[r, c], sea_level_m, side_m)
            if measures is not None and np.mean(field[r, c] < level) <= MAX_CITY_SEA:
                city_rows.append(measures)
                z = np.r_[(measures - city_mean) / city_sd,
                          ((np.array(window_stat) - city_tex_mean) / city_tex_sd)[[0, 1, 4]]]
                candidates.append((float(np.sqrt(np.nanmean(z**2))), measures))
        if candidates:
            best.append(min(candidates, key=lambda c: c[0]))
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
        "",
        f"Cities between sea and mountains, at a {scale_m:.0f} m vertical scale"
        f"{' (calibrated)' if args.at_calibrated_scale else ''} and {sea_level_m:g} m sea level:",
        f"{'':22s}" + "".join(f"{name:>13s}" for name in CITY_NAMES) + "     distance",
        f"{'real cities':22s}" + "".join(f"{m:13.2f}" for m in city_mean),
        f"{'all windows (median)':22s}" + "".join(f"{m:13.2f}" for m in np.median(city_rows, axis=0)),
        f"{'most city-like':22s}" + "".join(f"{m:13.2f}" for m in np.mean([b[1] for b in best], axis=0))
        + f"{np.mean([b[0] for b in best]):13.2f}",
        f"vertical scale at which highland slopes match the cities' ({city_mean[HIGH_SLOPE]:.1f}°): {calibrated:.0f} m; "
        f"highest point at that scale {max(HeightMapping(calibrated, sea_level_m, lv).metres(f.max()) for f, lv in worlds):.0f} m "
        f"(mean of worlds' highest {np.mean([HeightMapping(calibrated, sea_level_m, lv).metres(f.max()) for f, lv in worlds]):.0f} m)",
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

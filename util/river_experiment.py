"""Tune river carving against real-terrain statistics.

Generates one map per seed from ``config.yaml`` (cached, since generation is
the slow part), then applies each configured variant: either draining only,
or river carving (``app.rivers.carve_rivers``, which drains before and after)
with overridden parameters. For every result it computes the scale-free
terrain statistics (``util.terrain_stats.terrain_stats``) and channel
concavity, compares their means with the same statistics on real-terrain
crops, and ranks the variants. (The depression fraction is not used: both
pipelines drain the map, so it would be zero by construction.) Writes a summary, per-map CSV and a contact sheet to
``output/experiments/``.
"""

import argparse
import csv
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml
from matplotlib.colors import LightSource

# Make the project root importable when run as a script.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config
from app.config import load_config
from app.drainage import fill_hollows
from app.pipeline import generate_height_field
from app.rivers import carve_rivers
from app.sea import sea_level
from util.reference_terrain import square_crops
from util.terrain_stats import drainage_stats
from util.terrain_stats import terrain_stats

# Default experiment definition, next to this script.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "river_experiment.yaml"
# Project root; relative paths in the experiment YAML are resolved against it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Where results and the map cache are written.
OUTPUT_DIR = PROJECT_ROOT / "output" / "experiments"
CACHE_DIR = OUTPUT_DIR / "map_cache"
# Statistics compared with nature, in output order.
STAT_NAMES = ["spectral_slope", "roughness", "hypsometric_integral", "skewness", "concavity"]


def cached_map(config: Config, seed: int) -> np.ndarray:
    """The generated height field (before draining or rivers) for ``seed``, generated from ``config`` once and then loaded from the cache.

    The cache key includes the settings that affect generation, so changing
    them regenerates the map.
    """
    key = abs(hash((seed, config.layers, config.composite, config.walk, config.deposit, config.plot.domain,
                    config.plot.grid_points, config.plot.cutoff))) % 10**12
    path = CACHE_DIR / f"seed{seed}_{key}.npy"
    if path.exists():
        return np.load(path)
    _, _, field = generate_height_field(config, seed)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    np.save(path, field)
    return field


def run_pipeline(field: np.ndarray, config: Config, variant: dict[str, Any], seed: int) -> np.ndarray:
    """Apply one variant to ``field``: drain only, or carve rivers with the variant's parameters."""
    level = sea_level(field, config.sea.water_fraction)
    if not variant["rivers"]:
        return fill_hollows(field, field < level, periodic=True, epsilon=config.drainage.epsilon)
    params = replace(config.rivers.params, **variant["river_params"])
    carved, _, _, _ = carve_rivers(field, level, params, seed, epsilon=config.drainage.epsilon)
    return carved


def _task(args: tuple[str, int, np.ndarray, dict[str, Any], str]) -> tuple[str, int, list[float], np.ndarray]:
    """Worker: run one variant on one map and return its statistics and the final map."""
    name, seed, field, variant, config_path = args
    config = load_config(config_path)
    final = run_pipeline(field, config, variant, seed)
    sea = final < sea_level(field, config.sea.water_fraction)
    stats = asdict(terrain_stats(final))
    stats["concavity"] = drainage_stats(final, sea, periodic=True).concavity
    return name, seed, [stats[k] for k in STAT_NAMES], final


def nature_stats(ref: dict[str, Any], grid_points: int) -> np.ndarray:
    """Statistics of every reference crop, shape (crops, len(STAT_NAMES))."""
    rng = np.random.default_rng(0)
    dem_dir = Path(ref["dem_dir"])
    if not dem_dir.is_absolute():
        dem_dir = PROJECT_ROOT / dem_dir
    rows = []
    for tile in sorted(dem_dir.glob("Copernicus_DSM_COG_10_*_DEM.tif")):
        for crop in square_crops(tile, ref["side_m"], grid_points, ref["crops_per_tile"], rng):
            stats = asdict(terrain_stats(crop))
            stats.update(asdict(drainage_stats(crop, np.zeros(crop.shape, dtype=bool), periodic=False)))
            rows.append([stats[k] for k in STAT_NAMES])
    if not rows:
        raise FileNotFoundError(f"no Copernicus tiles found in {dem_dir}")
    return np.array(rows)


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Tune river carving against real-terrain statistics.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Experiment YAML file.")
    parser.add_argument("--app-config", type=Path, default=PROJECT_ROOT / "config.yaml", help="App config for generation and river defaults.")
    return parser.parse_args()


def main() -> None:
    """Run every variant on every cached map, compare with nature, and write the results."""
    args = _parse_args()
    with args.config.open(encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    config = load_config(args.app_config)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = exp["name"]

    nature = nature_stats(exp["reference"], config.plot.grid_points)
    nat_mean, nat_sd = nature.mean(axis=0), nature.std(axis=0)

    maps = {seed: cached_map(config, seed) for seed in exp["seeds"]}
    variants = {}
    for name, overrides in exp["configs"].items():
        v = {**exp["baseline"], **overrides}
        v["river_params"] = {**exp["baseline"]["river_params"], **overrides.get("river_params", {})}
        variants[name] = v
    tasks = [(name, seed, maps[seed], v, str(args.app_config)) for name, v in variants.items() for seed in exp["seeds"]]
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_task, tasks))

    per_variant: dict[str, list[list[float]]] = {name: [] for name in variants}
    first_maps: dict[str, np.ndarray] = {}
    with (OUTPUT_DIR / f"{prefix}_maps.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["config", "seed", *STAT_NAMES])
        for name, seed, values, final in results:
            per_variant[name].append(values)
            writer.writerow([name, seed, *values])
            if seed == exp["seeds"][0]:
                first_maps[name] = final

    short = ["beta", "H", "HI", "skew", "theta"]
    lines = [f"{'config':<22}" + "".join(f"{s:>16}" for s in short) + f"{'distance':>10}",
             f"{'nature (%d)' % len(nature):<22}" + "".join(f"{m:>9.3f}±{s:<6.3f}" for m, s in zip(nat_mean, nat_sd)) + f"{0:>10.2f}"]
    ranked = []
    for name, rows in per_variant.items():
        arr = np.array(rows)
        mu, sd = arr.mean(axis=0), arr.std(axis=0)
        dist = float(np.sqrt(np.mean(((mu - nat_mean) / nat_sd) ** 2)))
        ranked.append((dist, name))
        lines.append(f"{name:<22}" + "".join(f"{a:>9.3f}±{b:<6.3f}" for a, b in zip(mu, sd)) + f"{dist:>10.2f}")
    lines.append("")
    lines.append("ranked by distance (RMS z-score over all five statistics): " + ", ".join(f"{n} ({d:.2f})" for d, n in sorted(ranked)))
    summary = "\n".join(lines)
    print(summary)
    (OUTPUT_DIR / f"{prefix}_summary.txt").write_text(summary + "\n", encoding="utf-8")

    cols = 4
    rows_n = math.ceil(len(first_maps) / cols)
    ls = LightSource(azdeg=315, altdeg=35)
    base = maps[exp["seeds"][0]]
    vmin, vmax = float(base.min()), float(base.max())
    fig, axes = plt.subplots(rows_n, cols, figsize=(4 * cols, 4 * rows_n))
    for ax in axes.ravel():
        ax.axis("off")
    for ax, (name, z) in zip(axes.ravel(), first_maps.items()):
        crop = z[100:250, 0:150]
        ax.imshow(ls.shade(crop, cmap=plt.cm.terrain, vert_exag=60, blend_mode="soft", vmin=vmin, vmax=vmax), origin="lower")
        ax.set_title(name, fontsize=9)
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / f"{prefix}_contact_sheet.png", dpi=70)
    plt.close(fig)
    print(f"Wrote results to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

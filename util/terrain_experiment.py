"""Compare composite-drunk height maps with real terrain under several configurations.

For each configuration in the experiment YAML, generates ``samples`` maps on
a fixed square domain, computes scale-free terrain statistics
(``util.terrain_stats``) for each, and compares their means with the same
statistics measured on square crops of real elevation data. Writes a CSV of
per-map statistics, a summary table (printed and saved), and a contact sheet
of the first map of every configuration to ``output/experiments/``, each
prefixed with the experiment's ``name``.
"""

import argparse
import csv
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import yaml
from PIL import Image

# Make the project root importable when run as a script.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.drunk import Drunk
from app.sampling import poisson_disk_points
from app.sampling import power_log_spacing
from util.reference_terrain import square_crops
from util.terrain_stats import TerrainStats
from util.terrain_stats import terrain_stats

# Default experiment definition, next to this script.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "terrain_experiment.yaml"
# Project root; relative paths in the experiment YAML are resolved against it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Where results are written.
OUTPUT_DIR = PROJECT_ROOT / "output" / "experiments"
# Statistic names, in TerrainStats field order.
STAT_NAMES = list(TerrainStats.__dataclass_fields__)


def expected_spread(kappa_max: float, r0: float, steps: int) -> float:
    """Approximate per-axis RMS spread of a drunk's path around home.

    Near home the bias acts like a spring, giving an equilibrium spread
    ``sqrt(r0 / (2 kappa_max))``; a weakly biased drunk never reaches it in
    ``steps`` steps and spreads like a free walk, ``sqrt(steps / 2)``. The two
    limits are combined harmonically.
    """
    s_eq = math.sqrt(r0 / (2.0 * kappa_max))
    s_free = math.sqrt(steps / 2.0)
    return 1.0 / math.sqrt(1.0 / s_eq**2 + 1.0 / s_free**2)


def levy_flight_points(n: int, side: float, alpha: float, min_step: float, rng: np.random.Generator) -> np.ndarray:
    """``n`` successive positions of a Lévy flight, wrapped into the square of side ``side`` centred on the origin.

    Step lengths follow a Pareto law ``min_step * U^(-1/alpha)`` (heavy-tailed:
    mostly short hops with occasional long jumps) in uniformly random
    directions, so the points form clusters within clusters. Smaller
    ``alpha`` gives longer jumps and looser clustering. Wrapping is periodic,
    so no points pile up at the square's edges. Returns shape (n, 2).
    """
    lengths = min_step * (1.0 - rng.random(n - 1)) ** (-1.0 / alpha)
    angles = rng.uniform(0.0, 2.0 * math.pi, n - 1)
    start = rng.uniform(-side / 2.0, side / 2.0, 2)
    steps = np.column_stack([lengths * np.cos(angles), lengths * np.sin(angles)])
    path = np.vstack([start, start + np.cumsum(steps, axis=0)])
    return (path + side / 2.0) % side - side / 2.0


def build_drunks(params: dict[str, Any], rng: np.random.Generator) -> list[Drunk]:
    """Create one map's drunks from ``params`` (a merged configuration) and ``rng``."""
    n = params["n"]
    seeds = [int(s) for s in rng.integers(0, 2**32, size=n)]
    if params["homes"] == "poisson":
        homes = poisson_disk_points(n, params["region"], rng)
    elif params["homes"] == "levy":
        homes = levy_flight_points(n, params["region"], params["levy_alpha"], params["levy_min_step"], rng)
    elif params["homes"] == "mixed":
        # Even Poisson-disk background coverage plus Lévy-clustered relief.
        n_levy = round(n * params["levy_fraction"])
        homes = np.vstack([
            poisson_disk_points(n - n_levy, params["region"], rng),
            levy_flight_points(n_levy, params["region"], params["levy_alpha"], params["levy_min_step"], rng),
        ])
        # Shuffle so kappa_max (assigned in order) isn't tied to the sampler.
        homes = homes[rng.permutation(n)]
    else:
        raise ValueError(f"unknown homes sampler {params['homes']!r}")
    kappas = power_log_spacing(params["kappa_start"], params["kappa_end"], n, params["kappa_power"])
    spreads = np.array([expected_spread(k, params["r0"], params["steps"]) for k in kappas])
    relative = spreads / spreads.min()
    amp_h = params["amp_h"]
    amplitudes = relative ** (2.0 + amp_h) if amp_h is not None else np.ones(n)
    variances = params["variance"] * relative ** (2.0 * params["var_q"])
    return [
        Drunk(
            seed,
            step_size=params["step_size"],
            kappa_max=float(k),
            r0=params["r0"],
            variance=float(v),
            decay=params["decay"],
            initial_amplitude=float(a),
            home=(float(h[0]), float(h[1])),
        )
        for seed, k, v, a, h in zip(seeds, kappas, variances, amplitudes, homes)
    ]


def layer_field(params: dict[str, Any], rng: np.random.Generator, domain: float, grid_points: int, cutoff: float) -> np.ndarray:
    """Walk one composite's drunks and return their summed field on a ``grid_points`` grid over the domain."""
    g = np.linspace(-domain / 2.0, domain / 2.0, grid_points)
    field = np.zeros((grid_points, grid_points))
    for drunk in build_drunks(params, rng):
        drunk.steps(params["steps"])
        field += drunk.density(g, g, cutoff)
    return field


def generate_map(params: dict[str, Any], seed: int, domain: float, grid_points: int, cutoff: float) -> np.ndarray:
    """Worker: build one map on the fixed domain grid.

    With ``octaves = 1`` this is a single composite's summed field. With more
    octaves, layer ``j`` is a composite scaled up by ``2^j`` (step size and
    ``r0`` by ``2^j``, deposit variance by ``4^j``); it is computed on a grid
    ``2^j`` times coarser (it is that much smoother), upsampled bilinearly,
    scaled to unit standard deviation and weighted by ``2^(j * octave_h)``, so
    relief grows with scale with exponent ``octave_h``.
    """
    rng = np.random.default_rng(seed)
    if params["octaves"] == 1:
        return layer_field(params, rng, domain, grid_points, cutoff)
    field = np.zeros((grid_points, grid_points))
    for j in range(params["octaves"]):
        scale = 2.0**j
        layer_params = {
            **params,
            "step_size": params["step_size"] * scale,
            "r0": params["r0"] * scale,
            "variance": params["variance"] * scale**2,
        }
        coarse = layer_field(layer_params, rng, domain, max(16, round(grid_points / scale)), cutoff)
        layer = np.asarray(
            Image.fromarray(coarse.astype(np.float32), mode="F").resize((grid_points, grid_points), Image.Resampling.BILINEAR),
            dtype=float,
        )
        field += scale ** params["octave_h"] * layer / layer.std()
    return field


def _map_task(args: tuple[str, int, dict[str, Any], int, float, int, float]) -> tuple[str, int, TerrainStats, np.ndarray]:
    """Worker: generate one map and its statistics; returns a thumbnail-sized copy for the contact sheet."""
    name, sample, params, seed, domain, grid_points, cutoff = args
    field = generate_map(params, seed, domain, grid_points, cutoff)
    return name, sample, terrain_stats(field), field[::4, ::4]


def reference_stats(ref: dict[str, Any], grid_points: int, seed: int) -> np.ndarray:
    """Statistics of every reference crop, shape (crops, statistics)."""
    rng = np.random.default_rng(seed)
    rows = []
    dem_dir = Path(ref["dem_dir"])
    if not dem_dir.is_absolute():
        dem_dir = PROJECT_ROOT / dem_dir
    for tile in sorted(dem_dir.glob("Copernicus_DSM_COG_10_*_DEM.tif")):
        for crop in square_crops(tile, ref["side_m"], grid_points, ref["crops_per_tile"], rng):
            rows.append(list(asdict(terrain_stats(crop)).values()))
    if not rows:
        raise FileNotFoundError(f"no Copernicus tiles found in {dem_dir}")
    return np.array(rows)


def distance_to_nature(means: np.ndarray, nature_mean: np.ndarray, nature_sd: np.ndarray) -> float:
    """RMS of the per-statistic z-scores of ``means`` against the natural distribution (0 = matches)."""
    return float(np.sqrt(np.mean(((means - nature_mean) / nature_sd) ** 2)))


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Compare composite-drunk height maps with real terrain statistics.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Experiment YAML file.")
    return parser.parse_args()


def main() -> None:
    """Run every configuration, compare with nature, and write the results."""
    args = _parse_args()
    with args.config.open(encoding="utf-8") as f:
        exp = yaml.safe_load(f)
    m = exp["map"]
    prefix = exp["name"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    nature = reference_stats(exp["reference"], m["grid_points"], exp["seed"])
    nature_mean, nature_sd = nature.mean(axis=0), nature.std(axis=0)

    tasks = []
    configs = {name: {**exp["baseline"], **overrides} for name, overrides in exp["configs"].items()}
    for name, params in configs.items():
        for i in range(exp["samples"]):
            tasks.append((name, i, params, exp["seed"] + i, m["domain"], m["grid_points"], m["cutoff"]))
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_map_task, tasks))

    per_config: dict[str, list[list[float]]] = {name: [] for name in configs}
    thumbs: dict[str, np.ndarray] = {}
    with (OUTPUT_DIR / f"{prefix}_maps.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["config", "sample", *STAT_NAMES])
        for name, sample, stats, thumb in results:
            values = list(asdict(stats).values())
            per_config[name].append(values)
            writer.writerow([name, sample, *values])
            if sample == 0:
                thumbs[name] = thumb

    header = f"{'config':<16}" + "".join(f"{s:>22}" for s in ("beta", "H", "HI", "skew")) + f"{'distance':>10}"
    lines = [header, f"{'nature':<16}" + "".join(f"{mu:>15.2f} ± {sd:<4.2f}" for mu, sd in zip(nature_mean, nature_sd)) + f"{0:>10.2f}"]
    ranked = []
    for name, rows in per_config.items():
        arr = np.array(rows)
        mu, sd = arr.mean(axis=0), arr.std(axis=0)
        dist = distance_to_nature(mu, nature_mean, nature_sd)
        ranked.append((dist, name))
        lines.append(f"{name:<16}" + "".join(f"{a:>15.2f} ± {b:<4.2f}" for a, b in zip(mu, sd)) + f"{dist:>10.2f}")
    lines.append("")
    lines.append("ranked by distance: " + ", ".join(f"{n} ({d:.2f})" for d, n in sorted(ranked)))
    summary = "\n".join(lines)
    print(summary)
    (OUTPUT_DIR / f"{prefix}_summary.txt").write_text(summary + "\n", encoding="utf-8")

    cols = 4
    rows_n = math.ceil(len(thumbs) / cols)
    fig, axes = plt.subplots(rows_n, cols, figsize=(3 * cols, 3 * rows_n))
    for ax in axes.ravel():
        ax.axis("off")
    for ax, (name, thumb) in zip(axes.ravel(), thumbs.items()):
        ax.imshow(thumb, origin="lower", cmap="terrain")
        ax.set_title(name, fontsize=9)
    fig.tight_layout()
    fig.savefig(OUTPUT_DIR / f"{prefix}_contact_sheet.png", dpi=90)
    plt.close(fig)
    print(f"Wrote results to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

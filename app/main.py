"""Entry point: simulate CompositeDrunks of several sizes and save an image of each one's combined deposits.

For each configured size, the composite's members are identical except for a
random seed and a kappa_max with power-log spacing across the same configured
range. A master RNG seeded from ``config.yaml`` generates all member seeds, so
the whole run is reproducible from that single setting.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

# When run as a script (python app/main.py) rather than a module (python -m app.main),
# put the project root on sys.path so the `app` package can be imported.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.composite_drunk import CompositeDrunk
from app.drunk import Drunk


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Simulate composite drunks' random walks and save their deposit heatmaps as PNGs.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the YAML config file (default: config.yaml at the project root).",
    )
    return parser.parse_args()


def _power_log_spacing(start: float, end: float, n: int, power: float) -> np.ndarray:
    """Return ``n`` values from ``start`` to ``end`` spaced as ``start * (end / start) ** (t ** power)``.

    ``t`` runs evenly from 0 to 1. ``power = 1`` is logarithmic spacing
    (``np.geomspace``); ``power > 1`` crowds more values towards ``start``,
    ``power < 1`` towards ``end``. Both ends must be positive.
    """
    if start <= 0 or end <= 0:
        raise ValueError(f"power-log spacing needs positive ends, got {start} and {end}")
    if power <= 0:
        raise ValueError(f"power-log spacing needs a positive power, got {power}")
    t = np.linspace(0.0, 1.0, n)
    return start * (end / start) ** (t**power)


def _build_drunks(config: Config, n: int, rng: np.random.Generator) -> list[Drunk]:
    """Create ``n`` drunks, identical except for a seed drawn from ``rng`` and a power-log spaced kappa_max."""
    seeds = [int(s) for s in rng.integers(0, 2**32, size=n)]
    kappa_maxes = _power_log_spacing(
        config.composite.kappa_max_start,
        config.composite.kappa_max_end,
        n,
        config.composite.kappa_max_power,
    )
    return [
        Drunk(
            seed,
            step_size=config.walk.step_size,
            kappa_max=float(kappa_max),
            r0=config.walk.r0,
            variance=config.deposit.variance,
            decay=config.deposit.decay,
            initial_amplitude=config.deposit.initial_amplitude,
        )
        for seed, kappa_max in zip(seeds, kappa_maxes)
    ]


def main() -> None:
    """Load config, and for each composite size build, run and save a CompositeDrunk of power-log spaced kappa_max drunks."""
    args = _parse_args()
    config = load_config(args.config)

    rng = np.random.default_rng(config.seed)
    for size in config.composite.sizes:
        composite = CompositeDrunk(_build_drunks(config, size, rng), max_workers=config.parallel.max_workers)
        composite.steps(config.walk.num_steps)
        print(composite)
        filename = config.paths.output_dir / f"composite_{size}_drunks.png"
        composite.to_png(filename, grid_points=config.plot.grid_points, cutoff=config.plot.cutoff)
        print(f"Saved {filename}")


if __name__ == "__main__":
    main()

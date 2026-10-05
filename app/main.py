"""Entry point: simulate CompositeDrunks and save an image of each one's combined Gaussian deposits.

A master RNG seeded from ``config.yaml`` generates a distinct seed for every
member drunk, so the whole run is reproducible from that single setting.
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
from app.config import load_config
from app.composite_drunk import CompositeDrunk


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


def main() -> None:
    """Load config, build and run each CompositeDrunk, and save its combined deposit heatmap."""
    args = _parse_args()
    config = load_config(args.config)

    rng = np.random.default_rng(config.seed)
    for size in config.composite.sizes:
        seeds = [int(s) for s in rng.integers(0, 2**32, size=size)]
        composite = CompositeDrunk(
            seeds,
            step_size=config.walk.step_size,
            kappa_max=config.walk.kappa_max,
            r0=config.walk.r0,
            variance=config.deposit.variance,
            decay=config.deposit.decay,
            initial_amplitude=config.deposit.initial_amplitude,
            max_workers=config.parallel.max_workers,
        )
        composite.steps(config.walk.num_steps)
        print(composite)
        filename = config.paths.output_dir / f"composite_{size}_drunks.png"
        composite.to_png(filename, grid_points=config.plot.grid_points, cutoff=config.plot.cutoff)
        print(f"Saved {filename}")


if __name__ == "__main__":
    main()

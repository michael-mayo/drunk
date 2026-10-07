"""Entry point: build multi-scale terrain height maps from layers of composite drunks.

Each layer is a CompositeDrunk at one of the configured scales (step size and
r0 multiplied by the scale, deposit variance by its square). Within a layer
the drunks are identical except for a random seed, a home (Poisson-disk
sampled inside the map, and the point each drunk is biased back towards) and a
kappa_max with power-log spacing; every layer gets the same spacing. The map
wraps around at its edges. One map is built per seed in ``config.yaml``; that
seed drives the RNG that generates the map's drunk seeds and homes, so each
map is reproducible from its seed. Sea level is then set so that a configured
fraction of each map is water, river drunks carve graded river valleys (and
hollows are filled so all land drains to the sea), and the map is rendered.
"""

import argparse
import sys
from pathlib import Path

# When run as a script (python app/main.py) rather than a module (python -m app.main),
# put the project root on sys.path so the `app` package can be imported.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import load_config
from app.pipeline import generate_terrain
from app.rendering import save_terrain_map


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Build a multi-scale terrain height map from layers of composite drunks and save it as a PNG.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Path to the YAML config file (default: config.yaml at the project root).",
    )
    return parser.parse_args()


def main() -> None:
    """Load config and, for each seed, generate a layered height field, set its sea level, and save the map.

    Pipeline per map (``app.pipeline.generate_terrain``): build one composite
    layer per scale, walk the drunks, evaluate the combined wrap-around height
    field, then post-process: set sea level from ``sea.water_fraction``
    (heights unchanged), carve rivers with river drunks if enabled (which also
    drains the map), or else just fill hollows so all land drains to the sea.
    The map is then rendered.
    """
    args = _parse_args()
    config = load_config(args.config)

    for seed in config.seeds:
        result = generate_terrain(config, seed)
        print(f"seed {seed}: {result.terrain}")
        print(f"  sea level {result.sea_level:.3f}")
        if result.state == "rivers":
            print(f"  rivers: {result.rivers_carved} carved ({result.rivers_to_sea} reach the sea, "
                  f"{result.rivers_carved - result.rivers_to_sea} are tributaries); every land cell drains to the sea")
        elif result.state == "drained":
            print("  filled hollows: every land cell now drains to the sea")
        filename = config.paths.output_dir / f"terrain_seed{seed}.png"
        save_terrain_map(filename, result.field, result.grid, result.grid, result.title, result.sea_level,
                         rivers=result.river_area)
        print(f"Saved {filename}")

    host = "localhost" if config.ui.host in ("127.0.0.1", "0.0.0.0") else config.ui.host
    print(f"\nDone. For the interactive web UI, run `python -m app.ui` and visit http://{host}:{config.ui.port}/")


if __name__ == "__main__":
    main()

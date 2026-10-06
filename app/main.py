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

import numpy as np

# When run as a script (python app/main.py) rather than a module (python -m app.main),
# put the project root on sys.path so the `app` package can be imported.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.composite_drunk import CompositeDrunk
from app.drainage import fill_hollows
from app.drunk import Drunk
from app.layered_drunk import LayeredDrunk
from app.layered_drunk import periodic_axis
from app.rivers import carve_rivers
from app.rendering import save_terrain_map
from app.sampling import poisson_disk_points
from app.sampling import power_log_spacing
from app.sea import sea_level


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


def _build_layer(config: Config, scale: float, rng: np.random.Generator) -> CompositeDrunk:
    """Create one layer's composite at ``scale``.

    Its drunks are identical except for a seed and home drawn from ``rng`` and
    a power-log spaced kappa_max; step size and r0 are multiplied by ``scale``
    and deposit variance by ``scale**2``.
    """
    n = config.composite.drunks
    seeds = [int(s) for s in rng.integers(0, 2**32, size=n)]
    homes = poisson_disk_points(n, config.plot.domain, rng, periodic=True)
    kappa_maxes = power_log_spacing(
        config.composite.kappa_max_start,
        config.composite.kappa_max_end,
        n,
        config.composite.kappa_max_power,
    )
    drunks = [
        Drunk(
            seed,
            step_size=config.walk.step_size * scale,
            kappa_max=float(kappa_max),
            r0=config.walk.r0 * scale,
            variance=config.deposit.variance * scale**2,
            decay=config.deposit.decay,
            initial_amplitude=config.deposit.initial_amplitude,
            home=(float(home[0]), float(home[1])),
        )
        for seed, kappa_max, home in zip(seeds, kappa_maxes, homes)
    ]
    return CompositeDrunk(drunks, max_workers=config.parallel.max_workers)


def main() -> None:
    """Load config and, for each seed, generate a layered height field, set its sea level, and save the map.

    Pipeline per map: build one composite layer per scale, walk the drunks,
    evaluate the combined wrap-around height field, then post-process: set sea
    level from ``sea.water_fraction`` (heights unchanged), carve rivers with
    river drunks if enabled (which also drains the map), or else just fill
    hollows so all land drains to the sea, and render.
    """
    args = _parse_args()
    config = load_config(args.config)
    grid = periodic_axis(config.plot.domain, config.plot.grid_points)

    for seed in config.seeds:
        rng = np.random.default_rng(seed)
        layers = [_build_layer(config, scale, rng) for scale in config.layers.scales]
        terrain = LayeredDrunk(layers, config.layers.scales, config.layers.h, max_workers=config.parallel.max_workers)
        terrain.steps(config.walk.num_steps)
        print(f"seed {seed}: {terrain}")

        field = terrain.density(grid, grid, config.plot.cutoff, period=config.plot.domain)
        level = sea_level(field, config.sea.water_fraction)
        print(f"  sea level {level:.3f} ({np.mean(field < level):.1%} of the map is sea)")

        river_area = None
        if config.rivers.enabled:
            field, river_area, carved, to_sea = carve_rivers(
                field, level, config.rivers.params, seed, epsilon=config.drainage.epsilon
            )
            print(f"  rivers: {carved} carved ({to_sea} reach the sea, {carved - to_sea} are tributaries); "
                  f"every land cell drains to the sea")
        elif config.drainage.fill:
            field = fill_hollows(field, field < level, periodic=True, epsilon=config.drainage.epsilon)
            print("  filled hollows: every land cell now drains to the sea")

        filename = config.paths.output_dir / f"terrain_seed{seed}.png"
        state = "rivers" if config.rivers.enabled else ("drained" if config.drainage.fill else "raw")
        title = f"seed {seed} ({state}): sea level {level:.3f}, {np.mean(field < level):.0%} sea"
        save_terrain_map(filename, field, grid, grid, title, level, rivers=river_area)
        print(f"Saved {filename}")


if __name__ == "__main__":
    main()

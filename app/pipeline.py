"""The terrain pipeline shared by the command line (``main``) and the web UI (``ui``).

For one seed: build one composite layer of drunks per scale, walk them,
evaluate the combined wrap-around height field, set sea level, then carve
rivers (which also drains the map) or just fill hollows. Each stage can
report progress through an optional callback.
"""

from collections.abc import Callable
from dataclasses import dataclass

import numba
import numpy as np

from app.composite_drunk import CompositeDrunk
from app.config import Config
from app.drainage import fill_hollows
from app.drunk import Drunk
from app.layered_drunk import LayeredDrunk
from app.layered_drunk import periodic_axis
from app.rivers import carve_rivers
from app.sampling import poisson_disk_points
from app.sampling import power_log_spacing
from app.sea import sea_level

# Share of the progress bar given to each stage, in order; they sum to 1.
# (Measured on a 400 x 400 map: sampling homes in Python takes about as long
# as evaluating the deposits in numba; walking is quick.)
_STAGE_SHARES = {"build": 0.25, "walk": 0.10, "density": 0.45, "sea": 0.01, "rivers": 0.19}


@dataclass(frozen=True)
class TerrainResult:
    """One generated map and what is needed to describe and draw it."""

    seed: int
    terrain: LayeredDrunk
    # Final height field (normalised) on the square grid ``grid`` x ``grid``.
    field: np.ndarray
    grid: np.ndarray
    sea_level: float
    # Each river cell's catchment area (0 elsewhere), or None if rivers are off.
    river_area: np.ndarray | None
    rivers_carved: int
    rivers_to_sea: int
    # "rivers", "drained", "raw", or "no sea" (nothing to drain to, so no draining or rivers).
    state: str

    @property
    def sea_fraction(self) -> float:
        """Fraction of the map below sea level."""
        return float(np.mean(self.field < self.sea_level))

    @property
    def title(self) -> str:
        """Title for the rendered map."""
        return f"seed {self.seed} ({self.state}): sea level {self.sea_level:.3f}, {self.sea_fraction:.0%} sea"


def build_layer(config: Config, scale: float, rng: np.random.Generator) -> CompositeDrunk:
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
    return CompositeDrunk(drunks)


def set_threads(config: Config) -> None:
    """Limit numba's parallel kernels, on the calling thread, to ``parallel.threads`` (null = every core)."""
    threads = config.parallel.threads
    numba.set_num_threads(numba.config.NUMBA_NUM_THREADS if threads is None else min(threads, numba.config.NUMBA_NUM_THREADS))


def generate_height_field(
    config: Config,
    seed: int,
    progress: Callable[[str, float, str], None] | None = None,
) -> tuple[LayeredDrunk, np.ndarray, np.ndarray]:
    """Build, walk and evaluate the layered drunks for ``seed``; returns ``(terrain, grid axis, height field)``.

    The field is the raw wrap-around height map, scaled to [0, 1], before sea
    level, draining or rivers. ``progress``, if given, is called with a stage
    name ("build", "walk" or "density"), that stage's fraction done and a
    message.
    """

    def report(stage: str) -> Callable[[float, str], None]:
        """Callback tagging a stage's own progress with its name."""

        def callback(fraction: float, message: str) -> None:
            if progress is not None:
                progress(stage, fraction, message)

        return callback

    set_threads(config)
    report("build")(0.0, "building layers of drunks")
    rng = np.random.default_rng(seed)
    layers = [build_layer(config, scale, rng) for scale in config.layers.scales]
    terrain = LayeredDrunk(layers, config.layers.scales, config.layers.h)

    report("walk")(0.0, "walking drunks")
    terrain.walk(config.walk.num_steps, progress=report("walk"))

    report("density")(0.0, "evaluating Gaussian deposits")
    field = terrain.density(config.plot.domain, config.plot.grid_points, config.plot.cutoff, progress=report("density"))
    return terrain, periodic_axis(config.plot.domain, config.plot.grid_points), field


def generate_terrain(
    config: Config,
    seed: int,
    progress: Callable[[float, str], None] | None = None,
) -> TerrainResult:
    """Run the whole pipeline for ``seed``.

    ``progress``, if given, is called with the overall fraction done (0 to 1)
    and a short message as each stage advances.
    """
    starts = {}
    total = 0.0
    for stage, share in _STAGE_SHARES.items():
        starts[stage] = total
        total += share

    def report(stage: str, fraction: float, message: str) -> None:
        """Map a stage's own 0-1 progress onto the overall bar."""
        if progress is not None:
            progress(starts[stage] + _STAGE_SHARES[stage] * fraction, message)

    terrain, grid, field = generate_height_field(config, seed, report)

    report("sea", 0.0, "setting sea level")
    level = sea_level(field, config.sea.water_fraction)

    river_area = None
    carved = to_sea = 0
    if not np.any(field < level):
        # A wrap-around map with no sea has no outlet: water can't drain anywhere,
        # so draining and river carving are skipped.
        state = "no sea"
    elif config.rivers.enabled:
        report("rivers", 0.0, "carving rivers")
        field, river_area, carved, to_sea = carve_rivers(
            field, level, config.rivers.params, seed, epsilon=config.drainage.epsilon
        )
        state = "rivers"
    elif config.drainage.fill:
        report("rivers", 0.0, "filling hollows")
        field = fill_hollows(field, field < level, periodic=True, epsilon=config.drainage.epsilon)
        state = "drained"
    else:
        state = "raw"
    report("rivers", 1.0, "terrain done")
    return TerrainResult(seed, terrain, field, grid, level, river_area, carved, to_sea, state)

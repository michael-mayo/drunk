"""The terrain pipeline shared by the command line (``main``) and the web UI (``ui``).

For one seed: build one composite layer of drunks per scale, walk them,
evaluate the combined wrap-around height field, set sea level, then carve
rivers (which also drains the map) or just fill hollows. Each stage can
report progress through an optional callback.
"""

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace

import numba
import numpy as np

from app.composite_drunk import CompositeDrunk
from app.config import Config
from app.config import LayerConfig
from app.drainage import fill_hollows
from app.drunk import Drunk
from app.layered_drunk import LayeredDrunk
from app.rivers import carve_rivers
from app.sampling import poisson_disk_points
from app.sampling import power_log_spacing
from app.sea import sea_level

# Share of the progress bar given to each stage, in order; they sum to 1.
# (Measured on the 1600 x 1600 world map: evaluating the deposits and
# draining and carving rivers take about the same time; the rest is quick.)
_STAGE_SHARES = {"build": 0.02, "walk": 0.04, "density": 0.47, "sea": 0.01, "rivers": 0.46}


@dataclass(frozen=True)
class TerrainResult:
    """One generated map and what is needed to describe and draw it."""

    seed: int
    terrain: LayeredDrunk
    # Final height field (normalised), indexed [y, x], on the wrap-around map.
    field: np.ndarray
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

    def centred_on(self, cx: int, cy: int) -> "TerrainResult":
        """The same map rolled round (it wraps) so that cell ``(cx, cy)`` (column, row) is at the centre.

        The centre is cell ``(n // 2, n // 2)``; the playable area is the
        square around it. Every cell keeps its height and neighbours, so the
        terrain, sea and rivers are unchanged, only where the map's edges fall.
        """
        n_y, n_x = self.field.shape
        shift = (n_y // 2 - cy, n_x // 2 - cx)
        river_area = None if self.river_area is None else np.roll(self.river_area, shift, axis=(0, 1))
        return replace(self, field=np.roll(self.field, shift, axis=(0, 1)), river_area=river_area)


def playable_crop(config: Config, field: np.ndarray) -> np.ndarray:
    """The playable area (``cs2.playable_width_km`` across) at the centre of a world-map field.

    For the default config this is the central 400 x 400 of the 1600 x 1600
    world: the area the terrain statistics were tuned on.
    """
    n = field.shape[0]
    half = round(n * config.cs2.playable_width_km / config.cs2.world_width_km / 2)
    return field[n // 2 - half : n // 2 + half, n // 2 - half : n // 2 + half]


def layer_drunks(config: Config, layer: LayerConfig, rng: np.random.Generator) -> list[Drunk]:
    """Create the drunks for one ``layer`` of the height map, at evenly spread (Poisson-disk) homes.

    Its drunks are identical except for a seed and home drawn from ``rng`` and
    a power-log spaced kappa_max; step size and r0 are multiplied by the
    layer's scale and deposit variance by its square.
    """
    n = layer.drunks
    scale = layer.scale
    seeds = [int(s) for s in rng.integers(0, 2**32, size=n)]
    homes = poisson_disk_points(n, config.plot.domain, rng, periodic=True)
    kappa_maxes = power_log_spacing(layer.kappa_max_start, layer.kappa_max_end, n, layer.kappa_max_power)
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
    return drunks


def set_threads(config: Config) -> None:
    """Limit numba's parallel kernels, on the calling thread, to ``parallel.threads`` (null = every core)."""
    threads = config.parallel.threads
    numba.set_num_threads(numba.config.NUMBA_NUM_THREADS if threads is None else min(threads, numba.config.NUMBA_NUM_THREADS))


def generate_height_field(
    config: Config,
    seed: int,
    progress: Callable[[str, float, str], None] | None = None,
) -> tuple[LayeredDrunk, np.ndarray]:
    """Build, walk and evaluate the layered drunks for ``seed``; returns ``(terrain, height field)``.

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
    drunks = [layer_drunks(config, layer, rng) for layer in config.layers]
    scales = [layer.scale for layer in config.layers]
    weights = [layer.weight for layer in config.layers]
    finest = min(scales)
    composites: list[CompositeDrunk | None] = [None] * len(drunks)
    references: list[CompositeDrunk | None] = [None] * len(drunks)
    fields: list[np.ndarray | None] = [None] * len(drunks)

    # Largest scale first, so each layer's parent (the next larger layer) has walked before its drunks are born.
    order = sorted(range(len(drunks)), key=lambda j: -scales[j])
    birth_rng = np.random.default_rng([seed, 1])
    report("walk")(0.0, "walking drunks")
    report("density")(0.0, "evaluating Gaussian deposits")
    for done, j in enumerate(order):
        layer = config.layers[j]
        parent = parent_layer(config.layers, j)
        if layer.born_on_parent > 0 and parent is not None:
            # The same drunks spread evenly (their Poisson-disk homes) scale the layer.
            references[j] = CompositeDrunk(drunks[j])
            composites[j] = CompositeDrunk(born_on_paths(drunks[j], composites[parent], layer.born_on_parent,
                                                         layer.parent_bias_power, config.plot.domain, birth_rng))
        else:
            composites[j] = CompositeDrunk(drunks[j])
        single = LayeredDrunk([composites[j]], [scales[j]], [weights[j]], [references[j]], finest_scale=finest)
        single.walk(config.walk.num_steps)
        report("walk")((done + 1) / len(order), f"walked layer {done + 1} of {len(order)} (scale {scales[j]:g})")
        fields[j] = single.layer_fields(config.plot.domain, config.plot.grid_points, config.plot.cutoff)[0]
        report("density")((done + 1) / len(order), f"evaluated layer {done + 1} of {len(order)} (scale {scales[j]:g})")
    terrain = LayeredDrunk(composites, scales, weights, references, finest_scale=finest)
    total = np.sum(fields, axis=0)
    lo, hi = total.min(), total.max()
    return terrain, (total - lo) / (hi - lo) if hi > lo else np.zeros_like(total)


def parent_layer(layers: tuple[LayerConfig, ...], j: int) -> int | None:
    """Index of layer ``j``'s parent: the layer with the next larger scale (None for the largest)."""
    larger = [i for i, layer in enumerate(layers) if layer.scale > layers[j].scale]
    return min(larger, key=lambda i: layers[i].scale) if larger else None


def born_on_paths(
    drunks: list[Drunk],
    parent: CompositeDrunk,
    share: float,
    bias_power: float,
    domain: float,
    rng: np.random.Generator,
) -> list[Drunk]:
    """The same drunks with a ``share`` of them moved to homes on the walked ``parent`` drunks' paths.

    Each moved drunk picks a parent drunk with probability proportional to
    ``kappa_max ** bias_power`` (0 = any parent alike), then one of that
    parent's deposits with probability proportional to its amplitude, and
    takes the deposit's centre, wrapped into the map of side ``domain``, as
    its home. Strongly biased parents stay near home and pile up deposits
    into peaks; weakly biased ones wander widely, lowlands included. So the
    higher ``bias_power``, the more the moved drunks, and their texture,
    gather on the mountains. The rest keep their evenly spread homes.
    Everything else about each drunk (seed, bias, step and deposit sizes) is
    unchanged.
    """
    moved = rng.random(len(drunks)) < share
    deposits = parent.deposits
    steps = parent.num_steps
    bias = np.array([d.kappa_max for d in parent.drunks]) ** bias_power
    weights = np.repeat(bias, steps) * deposits.amplitude
    picks = rng.choice(len(weights), size=int(moved.sum()), p=weights / weights.sum())
    xs = (deposits.x[picks] + domain / 2.0) % domain - domain / 2.0
    ys = (deposits.y[picks] + domain / 2.0) % domain - domain / 2.0
    born = []
    k = 0
    for drunk, move in zip(drunks, moved):
        if move:
            born.append(replace(drunk, home=(float(xs[k]), float(ys[k]))))
            k += 1
        else:
            born.append(drunk)
    return born


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

    terrain, field = generate_height_field(config, seed, report)

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
    return TerrainResult(seed, terrain, field, level, river_area, carved, to_sea, state)

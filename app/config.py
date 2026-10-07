"""Load and validate project settings from config.yaml.

All key parameters are defined in a YAML file and exposed as frozen
dataclasses, so the rest of the code receives a typed, read-only ``Config``
object instead of reading raw dicts or hard-coding values.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.rivers import RiverParams

# Default config file location: config.yaml at the project root.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass(frozen=True)
class LayersConfig:
    """Scales of the layers in main.py's height map, and their weighting exponent."""

    scales: tuple[float, ...]
    h: float


@dataclass(frozen=True)
class CompositeConfig:
    """Each layer's composite: number of drunks and kappa_max spacing."""

    drunks: int
    kappa_max_start: float
    kappa_max_end: float
    kappa_max_power: float


@dataclass(frozen=True)
class ParallelConfig:
    """Parallel-processing settings."""

    # Threads used by the numba kernels (None = every core).
    threads: int | None


@dataclass(frozen=True)
class WalkConfig:
    """Parameters of the random-walk simulation."""

    num_steps: int
    step_size: float
    r0: float


@dataclass(frozen=True)
class DepositConfig:
    """Parameters of the Gaussian deposited after each step."""

    variance: float
    initial_amplitude: float
    decay: float


@dataclass(frozen=True)
class PlotConfig:
    """Settings for rendering images."""

    domain: float
    grid_points: int
    cutoff: float


@dataclass(frozen=True)
class SeaConfig:
    """Sea level, set from the fraction of the map that is water."""

    water_fraction: float


@dataclass(frozen=True)
class DrainageConfig:
    """Hollow filling, so all land drains to the sea."""

    fill: bool
    epsilon: float


@dataclass(frozen=True)
class RiversConfig:
    """Whether to carve rivers with river drunks, and their parameters."""

    enabled: bool
    params: RiverParams


@dataclass(frozen=True)
class Cs2Config:
    """Cities: Skylines II heightmap conventions."""

    # Height (m) spanned by the full 16-bit range at the editor's default height scale.
    max_height_m: float


@dataclass(frozen=True)
class UiConfig:
    """Web UI server settings, and the peak-height slider."""

    host: str
    port: int
    open_browser: bool
    peak_height_m: float
    peak_height_min_m: float
    peak_height_step_m: float
    sea_fraction_max: float


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem locations used by the project."""

    output_dir: Path


@dataclass(frozen=True)
class Config:
    """Top-level project configuration."""

    seeds: tuple[int, ...]
    layers: LayersConfig
    composite: CompositeConfig
    parallel: ParallelConfig
    walk: WalkConfig
    deposit: DepositConfig
    plot: PlotConfig
    sea: SeaConfig
    drainage: DrainageConfig
    rivers: RiversConfig
    cs2: Cs2Config
    ui: UiConfig
    paths: PathsConfig


def _resolve(base_dir: Path, value: str) -> Path:
    """Resolve a path from the config relative to the config file's directory."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base_dir / path).resolve()


def _require(section: dict[str, Any], key: str, section_name: str) -> Any:
    """Return ``section[key]``, raising a clear error if the key is missing."""
    if key not in section:
        name = f"{section_name}.{key}" if section_name else key
        raise KeyError(f"Missing required setting '{name}' in config")
    return section[key]


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Read a YAML config file and build a ``Config``.

    Every setting is required, so a missing key fails loudly at startup rather
    than silently falling back to a hidden default in code.
    """
    config_path = Path(path).resolve()
    with config_path.open(encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    base_dir = config_path.parent
    layers_raw = _require(raw, "layers", "")
    layers = LayersConfig(
        scales=tuple(float(s) for s in _require(layers_raw, "scales", "layers")),
        h=float(_require(layers_raw, "h", "layers")),
    )
    composite_raw = _require(raw, "composite", "")
    composite = CompositeConfig(
        drunks=int(_require(composite_raw, "drunks", "composite")),
        kappa_max_start=float(_require(composite_raw, "kappa_max_start", "composite")),
        kappa_max_end=float(_require(composite_raw, "kappa_max_end", "composite")),
        kappa_max_power=float(_require(composite_raw, "kappa_max_power", "composite")),
    )
    parallel_raw = _require(raw, "parallel", "")
    threads = _require(parallel_raw, "threads", "parallel")
    parallel = ParallelConfig(threads=None if threads is None else int(threads))
    if parallel.threads is not None and parallel.threads < 1:
        raise ValueError("config needs parallel.threads >= 1 (or null for every core)")
    walk_raw = _require(raw, "walk", "")
    walk = WalkConfig(
        num_steps=int(_require(walk_raw, "num_steps", "walk")),
        step_size=float(_require(walk_raw, "step_size", "walk")),
        r0=float(_require(walk_raw, "r0", "walk")),
    )
    deposit_raw = _require(raw, "deposit", "")
    deposit = DepositConfig(
        variance=float(_require(deposit_raw, "variance", "deposit")),
        initial_amplitude=float(_require(deposit_raw, "initial_amplitude", "deposit")),
        decay=float(_require(deposit_raw, "decay", "deposit")),
    )
    plot_raw = _require(raw, "plot", "")
    plot = PlotConfig(
        domain=float(_require(plot_raw, "domain", "plot")),
        grid_points=int(_require(plot_raw, "grid_points", "plot")),
        cutoff=float(_require(plot_raw, "cutoff", "plot")),
    )
    if not 0 < plot.cutoff < 1:
        raise ValueError("config needs 0 < plot.cutoff < 1")
    sea_raw = _require(raw, "sea", "")
    sea = SeaConfig(water_fraction=float(_require(sea_raw, "water_fraction", "sea")))
    drainage_raw = _require(raw, "drainage", "")
    drainage = DrainageConfig(
        fill=bool(_require(drainage_raw, "fill", "drainage")),
        epsilon=float(_require(drainage_raw, "epsilon", "drainage")),
    )
    rivers_raw = _require(raw, "rivers", "")
    float_keys = ("min_source_height", "direction_smoothing", "kappa", "inertia", "concavity", "valley_width", "min_valley_sigma")
    int_keys = ("sources", "stall_steps")
    rivers = RiversConfig(
        enabled=bool(_require(rivers_raw, "enabled", "rivers")),
        params=RiverParams(
            **{k: float(_require(rivers_raw, k, "rivers")) for k in float_keys},
            **{k: int(_require(rivers_raw, k, "rivers")) for k in int_keys},
        ),
    )
    cs2_raw = _require(raw, "cs2", "")
    cs2 = Cs2Config(max_height_m=float(_require(cs2_raw, "max_height_m", "cs2")))
    ui_raw = _require(raw, "ui", "")
    ui = UiConfig(
        host=str(_require(ui_raw, "host", "ui")),
        port=int(_require(ui_raw, "port", "ui")),
        open_browser=bool(_require(ui_raw, "open_browser", "ui")),
        peak_height_m=float(_require(ui_raw, "peak_height_m", "ui")),
        peak_height_min_m=float(_require(ui_raw, "peak_height_min_m", "ui")),
        peak_height_step_m=float(_require(ui_raw, "peak_height_step_m", "ui")),
        sea_fraction_max=float(_require(ui_raw, "sea_fraction_max", "ui")),
    )
    if not 0 <= sea.water_fraction <= ui.sea_fraction_max < 1:
        raise ValueError("config needs 0 <= sea.water_fraction <= ui.sea_fraction_max < 1")
    if not 0 < ui.peak_height_min_m <= ui.peak_height_m <= cs2.max_height_m:
        raise ValueError("config needs 0 < ui.peak_height_min_m <= ui.peak_height_m <= cs2.max_height_m")
    paths_raw = _require(raw, "paths", "")
    paths = PathsConfig(
        output_dir=_resolve(base_dir, _require(paths_raw, "output_dir", "paths")),
    )
    return Config(
        seeds=tuple(int(x) for x in _require(raw, "seeds", "")),
        layers=layers,
        composite=composite,
        parallel=parallel,
        walk=walk,
        deposit=deposit,
        plot=plot,
        sea=sea,
        drainage=drainage,
        rivers=rivers,
        cs2=cs2,
        ui=ui,
        paths=paths,
    )

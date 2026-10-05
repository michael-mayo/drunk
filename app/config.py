"""Load and validate project settings from config.yaml.

All key parameters are defined in a YAML file and exposed as frozen
dataclasses, so the rest of the code receives a typed, read-only ``Config``
object instead of reading raw dicts or hard-coding values.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Default config file location: config.yaml at the project root.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


@dataclass(frozen=True)
class CompositeConfig:
    """Which composite drunks to build."""

    sizes: tuple[int, ...]


@dataclass(frozen=True)
class ParallelConfig:
    """Parallel-processing settings."""

    max_workers: int | None


@dataclass(frozen=True)
class WalkConfig:
    """Parameters of the random-walk simulation."""

    num_steps: int
    step_size: float
    kappa_max: float
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

    grid_points: int
    cutoff: float


@dataclass(frozen=True)
class PathsConfig:
    """Filesystem locations used by the project."""

    sample_images_dir: Path
    output_dir: Path


@dataclass(frozen=True)
class Config:
    """Top-level project configuration."""

    seed: int
    composite: CompositeConfig
    parallel: ParallelConfig
    walk: WalkConfig
    deposit: DepositConfig
    plot: PlotConfig
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
    composite_raw = _require(raw, "composite", "")
    composite = CompositeConfig(sizes=tuple(int(n) for n in _require(composite_raw, "sizes", "composite")))
    parallel_raw = _require(raw, "parallel", "")
    max_workers = _require(parallel_raw, "max_workers", "parallel")
    parallel = ParallelConfig(max_workers=None if max_workers is None else int(max_workers))
    walk_raw = _require(raw, "walk", "")
    walk = WalkConfig(
        num_steps=int(_require(walk_raw, "num_steps", "walk")),
        step_size=float(_require(walk_raw, "step_size", "walk")),
        kappa_max=float(_require(walk_raw, "kappa_max", "walk")),
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
        grid_points=int(_require(plot_raw, "grid_points", "plot")),
        cutoff=float(_require(plot_raw, "cutoff", "plot")),
    )
    paths_raw = _require(raw, "paths", "")
    paths = PathsConfig(
        sample_images_dir=_resolve(base_dir, _require(paths_raw, "sample_images_dir", "paths")),
        output_dir=_resolve(base_dir, _require(paths_raw, "output_dir", "paths")),
    )
    return Config(
        seed=int(_require(raw, "seed", "")),
        composite=composite,
        parallel=parallel,
        walk=walk,
        deposit=deposit,
        plot=plot,
        paths=paths,
    )

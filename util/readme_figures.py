"""Draw the explanatory figures shown in the README.

Writes three PNGs to ``sample_images/`` (or ``--out``), all from the settings
in ``config.yaml``:

- ``drunk_walks.png``: three single drunks with weak to strong homeward bias,
  their paths drawn over the deposits they leave.
- ``layers.png``: the four layers of one map, each scaled and weighted as in
  the sum, and the combined height field.
- ``stages.png``: one map after each post-processing stage: sea level,
  drained, and rivers carved.

The seed maps shown in the README are written by ``app.main``.
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Make the project root importable when run as a script.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import DEFAULT_CONFIG_PATH
from app.config import Config
from app.config import load_config
from app.deposits import deposit_field
from app.drainage import fill_hollows
from app.drunk import Drunk
from app.drunk import walk_drunks
from app.pipeline import generate_height_field
from app.rendering import draw_rivers
from app.rendering import sea_and_land_colormap
from app.rivers import carve_rivers
from app.sea import sea_level

# Project root; the default output folder is relative to it.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Map used for the layer and stage figures.
FIGURE_SEED = 41
# Homeward bias of the three drunks in drunk_walks.png: the ends of the default range and a value between.
WALK_KAPPAS = (0.01, 0.1, 0.4)


def drunk_walks(config: Config, path: Path) -> None:
    """Three drunks at scale 1 with weak to strong bias: deposits as a heatmap, path as a line, home as a dot."""
    side = 80.0
    n = 320
    fig, axes = plt.subplots(1, len(WALK_KAPPAS), figsize=(12, 4.3))
    for ax, kappa_max in zip(axes, WALK_KAPPAS):
        drunk = Drunk(
            3,
            step_size=config.walk.step_size,
            kappa_max=kappa_max,
            r0=config.walk.r0,
            variance=config.deposit.variance,
            decay=config.deposit.decay,
            initial_amplitude=config.deposit.initial_amplitude,
        )
        d = walk_drunks([drunk], config.walk.num_steps)
        field = deposit_field(d, -side / 2, side / (n - 1), n, config.plot.cutoff, periodic=False)
        ax.imshow(field, origin="lower", extent=(-side / 2, side / 2, -side / 2, side / 2), cmap="magma")
        ax.plot(np.r_[0.0, d.x], np.r_[0.0, d.y], color="white", linewidth=0.4, alpha=0.6)
        ax.plot(0, 0, "o", color="cyan", markersize=5)
        ax.set_title(f"kappa_max = {kappa_max:g}")
        ax.set_xticks([])
        ax.set_yticks([])
    fig.suptitle(f"One drunk, {config.walk.num_steps} steps from home (cyan): path and summed deposits")
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def layers(config: Config, path: Path) -> None:
    """Each weighted layer of one map on a shared colour scale, and their sum scaled to [0, 1].

    Each layer is drawn minus its mean, which only shifts the sum by a
    constant (removed when it is scaled to [0, 1]), so the panels show relief
    rather than each layer's offset.
    """
    terrain, _, field = generate_height_field(config, FIGURE_SEED)
    fields = [f - f.mean() for f in terrain.layer_fields(config.plot.domain, config.plot.grid_points, config.plot.cutoff)]
    hi = max(np.abs(f).max() for f in fields)
    lo = -hi
    fig, axes = plt.subplots(1, len(fields) + 1, figsize=(3.2 * (len(fields) + 1), 3.6))
    for ax, f, scale in zip(axes, fields, terrain.scales):
        ax.imshow(f, origin="lower", cmap="terrain", vmin=lo, vmax=hi)
        ax.set_title(f"scale {scale:g} (weight {(scale / min(terrain.scales)) ** terrain.h:.2f})")
    axes[-1].imshow(field, origin="lower", cmap="terrain")
    axes[-1].set_title("sum, scaled to [0, 1]")
    for ax in axes:
        ax.set_xticks([])
        ax.set_yticks([])
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def stages(config: Config, path: Path) -> None:
    """One map after setting sea level, after draining (filled cells marked), and after carving rivers."""
    _, _, raw = generate_height_field(config, FIGURE_SEED)
    level = sea_level(raw, config.sea.water_fraction)
    sea = raw < level
    drained = fill_hollows(raw, sea, periodic=True, epsilon=config.drainage.epsilon)
    carved, river, _, _ = carve_rivers(raw, level, config.rivers.params, FIGURE_SEED, epsilon=config.drainage.epsilon)
    n = raw.shape[0]
    axis = np.arange(n)
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.3))
    titles = ("1. sea level (15% of the map)", "2. hollows filled (red) to drain", "3. rivers carved")
    for ax, f, title in zip(axes, (raw, drained, carved), titles):
        ax.imshow(f, origin="lower", cmap=sea_and_land_colormap(level, float(f.min()), float(f.max())))
        ax.contour(f, levels=[level], colors="navy", linewidths=0.5)
        ax.set_title(title)
        ax.set_xticks([])
        ax.set_yticks([])
    filled = np.ma.masked_where(drained - raw < 1e-4, drained - raw)
    axes[1].imshow(filled, origin="lower", cmap="Reds", vmin=0, vmax=float(filled.max()) * 0.5, alpha=0.8)
    draw_rivers(axes[2], river, axis, axis)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


def _parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Draw the README's explanatory figures.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="App config file (default: config.yaml).")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "sample_images", help="Output folder (default: sample_images/).")
    return parser.parse_args()


def main() -> None:
    """Draw every figure."""
    args = _parse_args()
    config = load_config(args.config)
    # The figures show rivers whatever the config says.
    config = replace(config, rivers=replace(config.rivers, enabled=True))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, draw in (("drunk_walks.png", drunk_walks), ("layers.png", layers), ("stages.png", stages)):
        draw(config, args.out / name)
        print(f"Saved {args.out / name}")


if __name__ == "__main__":
    main()

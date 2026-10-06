"""Render summed Gaussian-deposit fields as heatmap PNGs."""

from pathlib import Path

import matplotlib

# Non-interactive backend so images can be written without a display.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


def square_grid(points: np.ndarray, margin: float, grid_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Return 1D x and y axes of a square grid covering ``points`` (shape (N, 2)) plus ``margin``.

    The grid is square so the rendered image isn't distorted.
    """
    lo = points.min(axis=0)
    hi = points.max(axis=0)
    cx, cy = (lo + hi) / 2
    half = (hi - lo).max() / 2 + margin
    gx = np.linspace(cx - half, cx + half, grid_points)
    gy = np.linspace(cy - half, cy + half, grid_points)
    return gx, gy


def save_heatmap(
    filename: Path | str,
    field: np.ndarray,
    gx: np.ndarray,
    gy: np.ndarray,
    title: str,
    label: str = "summed deposit amplitude",
) -> None:
    """Save ``field`` (sampled on ``gx`` x ``gy``) as a heatmap, with ``label`` on the colour bar.

    Parent folders are created if needed.
    """
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(7, 6))
    image = ax.imshow(
        field,
        origin="lower",
        extent=(gx[0], gx[-1], gy[0], gy[-1]),
        cmap="magma",
        interpolation="bilinear",
    )
    fig.colorbar(image, ax=ax, label=label)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)

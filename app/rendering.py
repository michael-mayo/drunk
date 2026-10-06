"""Render summed Gaussian-deposit fields as heatmap PNGs."""

from pathlib import Path

import matplotlib

# Non-interactive backend so images can be written without a display.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap


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


def _sea_and_land_colormap(sea_level: float, vmin: float, vmax: float, n: int = 512) -> ListedColormap:
    """Colour map over ``[vmin, vmax]`` with blues below ``sea_level`` and terrain colours above.

    Sea gets darker with depth; land runs from lowland green up through brown
    to white peaks, so the coastline is where the two meet.
    """
    split = int(round(n * (sea_level - vmin) / (vmax - vmin))) if vmax > vmin else 0
    split = min(max(split, 0), n)
    sea = plt.get_cmap("Blues_r")(np.linspace(0.15, 0.75, split)) if split else np.empty((0, 4))
    # Skip the blue and sand at the bottom of matplotlib's "terrain" map.
    land = plt.get_cmap("terrain")(np.linspace(0.25, 1.0, n - split))
    return ListedColormap(np.vstack([sea, land]))


def _draw_rivers(ax: plt.Axes, area: np.ndarray, gx: np.ndarray, gy: np.ndarray) -> None:
    """Draw river cells as light-blue dots sized and shaded by log catchment area (larger rivers on top)."""
    iy, ix = np.nonzero(area > 0)
    a = np.log(area[iy, ix])
    t = (a - a.min()) / (a.max() - a.min()) if a.max() > a.min() else np.ones_like(a)
    order = np.argsort(t)
    # Dot diameter in points, roughly one grid cell at the smallest and ~3 cells for the largest rivers.
    cell_pt = ax.get_window_extent().width * 72.0 / ax.figure.dpi / len(gx)
    diameter = cell_pt * (0.8 + 2.2 * t[order])
    colours = np.zeros((len(order), 4))
    colours[:, :3] = (0.35, 0.75, 1.0)
    colours[:, 3] = 0.35 + 0.45 * t[order]
    ax.scatter(gx[ix[order]], gy[iy[order]], s=diameter**2, c=colours, marker="o", linewidths=0)


def save_terrain_map(
    filename: Path | str,
    field: np.ndarray,
    gx: np.ndarray,
    gy: np.ndarray,
    title: str,
    sea_level: float,
    rivers: np.ndarray | None = None,
) -> None:
    """Save ``field`` as a terrain map: sea below ``sea_level`` in blues, land above in terrain colours.

    The coastline is drawn as a thin contour, and the sea level is marked on
    the colour bar. If ``rivers`` is given (each river cell's catchment area,
    0 elsewhere), rivers are drawn as semi-transparent light-blue lines that
    widen and strengthen downstream with the log of catchment area, so they
    read as channels in the terrain rather than as sea. Parent folders are
    created if needed.
    """
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    vmin, vmax = float(field.min()), float(field.max())

    fig, ax = plt.subplots(figsize=(7, 6))
    extent = (gx[0], gx[-1], gy[0], gy[-1])
    image = ax.imshow(
        field,
        origin="lower",
        extent=extent,
        cmap=_sea_and_land_colormap(sea_level, vmin, vmax),
        vmin=vmin,
        vmax=vmax,
        interpolation="bilinear",
    )
    ax.contour(field, levels=[sea_level], colors="navy", linewidths=0.5, origin="lower", extent=extent)
    if rivers is not None and np.any(rivers > 0):
        _draw_rivers(ax, rivers, gx, gy)
    bar = fig.colorbar(image, ax=ax, label="normalised height")
    bar.ax.axhline(sea_level, color="black", linewidth=1.0)
    bar.ax.text(1.6, sea_level, "sea level", transform=bar.ax.get_yaxis_transform(), va="center", fontsize=8)
    ax.set_xlabel("x")
    ax.set_ylabel("y")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


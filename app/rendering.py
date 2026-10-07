"""Render height fields as terrain-map PNGs: sea, land and rivers."""

from pathlib import Path
from typing import BinaryIO

import matplotlib

# Non-interactive backend so images can be written without a display.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import ListedColormap
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator

# Size of the terrain-map figure, in inches.
FIGURE_SIZE = (7.0, 6.0)
# Where the map itself sits in the figure: (left, bottom, width, height) as
# fractions of the figure, measured from its lower-left corner. The width and
# height make an exact square in inches (0.66 x 7 = 0.77 x 6 = 4.62 in), so the
# map fills this box and a click on the image can be turned into a map position.
MAP_RECT = (0.09, 0.115, 0.66, 0.77)


def sea_and_land_colormap(sea_level: float, vmin: float, vmax: float, n: int = 512) -> ListedColormap:
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


def draw_rivers(ax: plt.Axes, area: np.ndarray, gx: np.ndarray, gy: np.ndarray) -> None:
    """Draw river cells as light-blue dots sized and shaded by log catchment area (larger rivers on top)."""
    iy, ix = np.nonzero(area > 0)
    a = np.log(area[iy, ix])
    t = (a - a.min()) / (a.max() - a.min()) if a.max() > a.min() else np.ones_like(a)
    order = np.argsort(t)
    # Dot diameter in points, roughly one grid cell at the smallest and ~3 cells for the largest rivers,
    # but never under 0.6 pt for a cell, so rivers stay visible on a fine grid.
    cell_pt = max(ax.get_window_extent().width * 72.0 / ax.figure.dpi / len(gx), 0.6)
    diameter = cell_pt * (0.8 + 2.2 * t[order])
    colours = np.zeros((len(order), 4))
    colours[:, :3] = (0.35, 0.75, 1.0)
    colours[:, 3] = 0.35 + 0.45 * t[order]
    ax.scatter(gx[ix[order]], gy[iy[order]], s=diameter**2, c=colours, marker="o", linewidths=0)


def save_terrain_map(
    filename: Path | str | BinaryIO,
    field: np.ndarray,
    title: str,
    sea_level: float,
    width_km: float,
    rivers: np.ndarray | None = None,
    height_scale_m: float | None = None,
    playable_km: float | None = None,
    dpi: int = 100,
) -> None:
    """Save ``field`` as a terrain map: sea below ``sea_level`` in blues, land above in terrain colours.

    The coastline is drawn as a thin contour, and the sea level is marked on
    the colour bar. If ``rivers`` is given (each river cell's catchment area,
    0 elsewhere), rivers are drawn as semi-transparent light-blue lines that
    widen and strengthen downstream with the log of catchment area, so they
    read as channels in the terrain rather than as sea. ``filename`` may also
    be an open binary file (e.g. ``io.BytesIO``), which receives the PNG.
    Parent folders of a path are created if needed.

    With ``height_scale_m``, heights are labelled in metres (normalised
    height x ``height_scale_m``): the colour bar gets round-metre ticks and
    the sea-level marker shows its height in metres. Only the labels change;
    the picture is identical for any scale.

    The map is drawn as a square ``width_km`` kilometres across, with both
    axes labelled from 0 km at the lower-left corner, at ``MAP_RECT`` in a
    ``FIGURE_SIZE`` figure saved at ``dpi``. With ``playable_km``, a square
    that many km across is outlined at the centre of the map: the playable
    area.
    """
    if isinstance(filename, (str, Path)):
        filename = Path(filename)
        filename.parent.mkdir(parents=True, exist_ok=True)
    vmin, vmax = float(field.min()), float(field.max())

    # Fixed axes positions (rather than tight_layout), so the map stays in exactly
    # the same place whatever the labels: only the numbers change between scales.
    fig = plt.figure(figsize=FIGURE_SIZE)
    ax = fig.add_axes(MAP_RECT)
    cax = fig.add_axes((0.80, 0.08, 0.03, 0.84))
    extent = (0.0, width_km, 0.0, width_km)
    # Cell centres in km, for placing the river dots.
    ny, nx = field.shape
    gx = (np.arange(nx) + 0.5) * width_km / nx
    gy = (np.arange(ny) + 0.5) * width_km / ny
    image = ax.imshow(
        field,
        origin="lower",
        extent=extent,
        cmap=sea_and_land_colormap(sea_level, vmin, vmax),
        vmin=vmin,
        vmax=vmax,
        interpolation="bilinear",
    )
    ax.contour(field, levels=[sea_level], colors="navy", linewidths=0.5, origin="lower", extent=extent)
    if rivers is not None and np.any(rivers > 0):
        draw_rivers(ax, rivers, gx, gy)
    if playable_km is not None:
        corner = (width_km - playable_km) / 2.0
        # A dark outline under a white one, so the square shows on sea and snow alike.
        for colour, width in (("black", 2.4), ("white", 1.2)):
            ax.add_patch(Rectangle((corner, corner), playable_km, playable_km, fill=False, edgecolor=colour,
                                   linewidth=width))
    if height_scale_m is None:
        bar = fig.colorbar(image, cax=cax, label="normalised height")
        sea_label = "sea level"
    else:
        bar = fig.colorbar(image, cax=cax, label="height (m)")
        # Ticks at round metre values, placed in the field's normalised units.
        ticks_m = MaxNLocator(nbins=7).tick_values(vmin * height_scale_m, vmax * height_scale_m)
        ticks_m = ticks_m[(ticks_m >= vmin * height_scale_m - 1e-9) & (ticks_m <= vmax * height_scale_m + 1e-9)]
        # Leave room for the sea-level label: drop ticks within 4% of the range of it.
        ticks_m = ticks_m[np.abs(ticks_m / height_scale_m - sea_level) > 0.04 * (vmax - vmin)]
        bar.set_ticks(ticks_m / height_scale_m)
        bar.set_ticklabels([f"{t:,.0f}" for t in ticks_m])
        sea_label = f"sea level\n{sea_level * height_scale_m:,.0f} m"
    # Put the bar's title on its left, leaving the right side for ticks and the
    # sea-level marker, which would otherwise collide when sea level is mid-range.
    bar.ax.yaxis.set_label_position("left")
    bar.ax.axhline(sea_level, color="black", linewidth=1.0)
    bar.ax.text(1.6, sea_level, sea_label, transform=bar.ax.get_yaxis_transform(), va="center", fontsize=8)
    ax.set_xlabel("x (km)")
    ax.set_ylabel("y (km)")
    ax.set_title(title)
    fig.savefig(filename, dpi=dpi, format="png")
    plt.close(fig)


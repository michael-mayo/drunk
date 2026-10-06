"""Multi-scale terrain: composites of drunks at several scales, combined into one height field."""

import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from app.composite_drunk import CompositeDrunk
from app.drunk import Drunk
from app.rendering import save_heatmap


def _density_sum(
    drunks: list[Drunk],
    gx: np.ndarray,
    gy: np.ndarray,
    cutoff: float,
    period: float | None = None,
) -> np.ndarray:
    """Worker: summed deposits of a batch of ``drunks`` on the grid ``gx`` x ``gy`` (runs in a child process).

    Returning one field per batch, rather than per drunk, keeps the data sent
    back to the parent small however many drunks there are.
    """
    field = np.zeros((len(gy), len(gx)))
    for drunk in drunks:
        field += drunk.density(gx, gy, cutoff, period)
    return field


def _resample(
    field: np.ndarray,
    src_x: np.ndarray,
    src_y: np.ndarray,
    dst_x: np.ndarray,
    dst_y: np.ndarray,
    period: float | None = None,
) -> np.ndarray:
    """Bilinearly resample ``field`` (shape ``(len(src_y), len(src_x))``) onto the grid ``dst_x`` x ``dst_y``.

    With ``period``, interpolation wraps around, so points past the last
    source sample blend back into the first.
    """
    rows = np.array([np.interp(dst_x, src_x, row, period=period) for row in field])
    return np.array([np.interp(dst_y, src_y, col, period=period) for col in rows.T]).T


def periodic_axis(domain: float, points: int) -> np.ndarray:
    """``points`` evenly spaced coordinates covering ``[-domain/2, domain/2)`` once, for a wrap-around map.

    The far edge is left out because it is the same line as the near edge.
    """
    return -domain / 2.0 + domain * np.arange(points) / points


class LayeredDrunk:
    """A height field built from ``CompositeDrunk`` layers at several scales.

    Each layer is a composite whose drunks are scaled by the layer's
    ``scale``: step size and ``r0`` multiplied by it, deposit variance by its
    square. ``kappa_max`` has no units, so a layer at scale ``s`` built from
    the same mix of drunks is a statistically exact ``s``-times enlargement of
    one at scale 1. The layers are combined as

        sum_j (s_j / s_min) ** h * L_j / std(L_j)

    then scaled linearly to [0, 1]. Scaling each layer to unit standard
    deviation makes the weights alone set how much relief each scale
    contributes, so height differences grow with distance roughly as
    ``lag ** h``, like natural terrain.

    A layer at scale ``s`` is ``s / s_min`` times smoother than the finest, so
    it is evaluated on a grid that much coarser and resampled bilinearly,
    which keeps coarse layers as cheap as fine ones.

    ``to_png`` renders a wrap-around map: deposits leaving one edge re-enter at
    the opposite one, so every point has neighbours on all sides (no thinning
    towards the edges), all deposits land on the map, and the map tiles
    seamlessly. Drunks' homes should then be sampled inside the map.
    """

    def __init__(self, layers: list[CompositeDrunk], scales: list[float], h: float, max_workers: int | None = None) -> None:
        """Combine ``layers`` (one composite per entry of ``scales``) with weighting exponent ``h``.

        ``max_workers`` caps the process pool used to evaluate the layers;
        ``None`` uses one worker per CPU.
        """
        if not layers or len(layers) != len(scales):
            raise ValueError("LayeredDrunk needs one or more layers, one per scale")
        if min(scales) <= 0:
            raise ValueError(f"layer scales must be positive, got {scales}")
        self.layers = list(layers)
        self.scales = [float(s) for s in scales]
        self.h = h
        self.max_workers = max_workers

    @property
    def num_steps(self) -> int:
        """Number of steps each drunk has taken."""
        return self.layers[0].num_steps

    def steps(self, n: int = 100) -> None:
        """Advance every drunk in every layer by ``n`` steps (each layer's members in parallel)."""
        for layer in self.layers:
            layer.steps(n)

    def _layer_grid(
        self,
        scale: float,
        gx: np.ndarray,
        gy: np.ndarray,
        period: float | None,
    ) -> tuple[np.ndarray, np.ndarray]:
        """The coarser grid, over the same area as ``gx`` x ``gy``, on which a layer at ``scale`` is evaluated."""
        factor = scale / min(self.scales)
        nx = max(16, round(len(gx) / factor))
        ny = max(16, round(len(gy) / factor))
        if period is not None:
            return gx[0] + period * np.arange(nx) / nx, gy[0] + period * np.arange(ny) / ny
        return np.linspace(gx[0], gx[-1], nx), np.linspace(gy[0], gy[-1], ny)

    def density(
        self,
        gx: np.ndarray,
        gy: np.ndarray,
        cutoff: float = 1 / 4096,
        period: float | None = None,
    ) -> np.ndarray:
        """The combined height field on the grid ``gx`` x ``gy``, scaled to [0, 1].

        With ``period``, the grid is one tile of a wrap-around map (see
        ``periodic_axis``). Every layer's drunks are evaluated in parallel, in
        batches. Batches are summed per layer in the parent in a fixed order,
        so the result is deterministic.
        """
        workers = self.max_workers or os.process_cpu_count() or 1
        tasks = []
        for j, (layer, scale) in enumerate(zip(self.layers, self.scales)):
            lx, ly = self._layer_grid(scale, gx, gy, period)
            for batch in np.array_split(np.arange(len(layer.drunks)), workers):
                if len(batch):
                    tasks.append((j, [layer.drunks[i] for i in batch], lx, ly))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            fields = list(pool.map(_density_sum, *zip(*[(d, lx, ly, cutoff, period) for _, d, lx, ly in tasks])))

        sums: dict[int, np.ndarray] = {}
        for (j, _, _, _), f in zip(tasks, fields):
            sums[j] = sums[j] + f if j in sums else f
        total = np.zeros((len(gy), len(gx)))
        for j, scale in enumerate(self.scales):
            lx, ly = self._layer_grid(scale, gx, gy, period)
            layer = _resample(sums[j], lx, ly, gx, gy, period)
            sd = layer.std()
            if sd > 0:
                total += (scale / min(self.scales)) ** self.h * layer / sd
        lo, hi = total.min(), total.max()
        return (total - lo) / (hi - lo) if hi > lo else np.zeros_like(total)

    def to_png(
        self,
        filename: Path | str,
        domain: float,
        grid_points: int = 400,
        cutoff: float = 1 / 4096,
        label: str = "",
    ) -> None:
        """Save a heatmap of the wrap-around map: the square of side ``domain`` centred on the origin.

        ``label``, if given, is prefixed to the title (e.g. the map's seed).
        """
        g = periodic_axis(domain, grid_points)
        field = self.density(g, g, cutoff, period=domain)
        n = sum(len(layer.drunks) for layer in self.layers)
        title = f"LayeredDrunk: {len(self.layers)} layers, {n} drunks, {self.num_steps} steps each"
        if label:
            title = f"{label}: {title}"
        save_heatmap(filename, field, g, g, title, label="normalised height")

    def __str__(self) -> str:
        """Summary of the layered field and of each layer."""
        lines = [f"LayeredDrunk(layers={len(self.layers)}, scales={self.scales}, h={self.h}, steps={self.num_steps})"]
        for scale, layer in zip(self.scales, self.layers):
            d = layer.drunks[0]
            lines.append(
                f"  scale={scale:g}: {len(layer.drunks)} drunks, step_size={d.step_size:g}, "
                f"r0={d.r0:g}, variance={d.variance:g}, kappa_max {min(x.kappa_max for x in layer.drunks):.4g}"
                f"-{max(x.kappa_max for x in layer.drunks):.4g}"
            )
        return "\n".join(lines)

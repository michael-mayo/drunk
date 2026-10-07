"""Multi-scale terrain: composites of drunks at several scales, combined into one height field."""

import math
from collections.abc import Callable

import numba
import numpy as np

from app.composite_drunk import CompositeDrunk


def periodic_axis(domain: float, points: int) -> np.ndarray:
    """``points`` evenly spaced coordinates covering ``[-domain/2, domain/2)`` once, for a wrap-around map.

    The far edge is left out because it is the same line as the near edge.
    """
    return -domain / 2.0 + domain * np.arange(points) / points


@numba.njit(parallel=True, cache=True)
def _resample_periodic(field: np.ndarray, n: int) -> np.ndarray:
    """Bilinearly resample the wrap-around square grid ``field`` onto an ``n`` x ``n`` grid over the same area.

    Both grids start at the same corner and leave out the far edge, so points
    past the last source sample blend back into the first.
    """
    m = field.shape[0]
    ratio = m / n
    out = np.empty((n, n))
    for i in numba.prange(n):
        fy = i * ratio
        y0 = int(math.floor(fy))
        wy = fy - y0
        y0 %= m
        y1 = (y0 + 1) % m
        for j in range(n):
            fx = j * ratio
            x0 = int(math.floor(fx))
            wx = fx - x0
            x0 %= m
            x1 = (x0 + 1) % m
            out[i, j] = ((field[y0, x0] * (1.0 - wx) + field[y0, x1] * wx) * (1.0 - wy)
                         + (field[y1, x0] * (1.0 - wx) + field[y1, x1] * wx) * wy)
    return out


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
    which keeps coarse layers as cheap as fine ones. The map wraps around:
    deposits leaving one edge re-enter at the opposite one, so the map tiles
    seamlessly. Drunks' homes should be sampled inside the map.
    """

    def __init__(self, layers: list[CompositeDrunk], scales: list[float], h: float) -> None:
        """Combine ``layers`` (one composite per entry of ``scales``) with weighting exponent ``h``."""
        if not layers or len(layers) != len(scales):
            raise ValueError("LayeredDrunk needs one or more layers, one per scale")
        if min(scales) <= 0:
            raise ValueError(f"layer scales must be positive, got {scales}")
        self.layers = list(layers)
        self.scales = [float(s) for s in scales]
        self.h = h

    @property
    def num_steps(self) -> int:
        """Number of steps each drunk has taken."""
        return self.layers[0].num_steps

    def walk(self, num_steps: int, progress: Callable[[float, str], None] | None = None) -> None:
        """Walk every drunk in every layer ``num_steps`` steps.

        ``progress``, if given, is called after each layer with the fraction
        of layers done and a short message.
        """
        for i, (layer, scale) in enumerate(zip(self.layers, self.scales)):
            layer.walk(num_steps)
            if progress is not None:
                progress((i + 1) / len(self.layers), f"walked layer {i + 1} of {len(self.layers)} (scale {scale:g})")

    def layer_grid_points(self, scale: float, grid_points: int) -> int:
        """Points per side of the coarser grid on which the layer at ``scale`` is evaluated."""
        return max(16, round(grid_points / (scale / min(self.scales))))

    def layer_fields(
        self,
        domain: float,
        grid_points: int,
        cutoff: float,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[np.ndarray]:
        """Each layer's weighted contribution to the height field, on a ``grid_points`` x ``grid_points`` wrap-around grid.

        Layer ``j`` is evaluated on its coarser grid, resampled, scaled to
        unit standard deviation and weighted by ``(s_j / s_min) ** h``; the
        height field is their sum. ``progress``, if given, is called after
        each layer with the fraction of layers done and a message.
        """
        fields = []
        for i, (layer, scale) in enumerate(zip(self.layers, self.scales)):
            coarse = layer.density(domain, self.layer_grid_points(scale, grid_points), cutoff)
            field = _resample_periodic(coarse, grid_points)
            sd = field.std()
            fields.append((scale / min(self.scales)) ** self.h * field / sd if sd > 0 else np.zeros_like(field))
            if progress is not None:
                progress((i + 1) / len(self.layers), f"evaluated layer {i + 1} of {len(self.layers)} (scale {scale:g})")
        return fields

    def density(
        self,
        domain: float,
        grid_points: int,
        cutoff: float,
        progress: Callable[[float, str], None] | None = None,
    ) -> np.ndarray:
        """The combined height field on a ``grid_points`` x ``grid_points`` wrap-around grid, scaled to [0, 1].

        The grid covers the square of side ``domain`` centred on the origin
        once (see ``periodic_axis``). ``progress`` is passed to ``layer_fields``.
        """
        total = np.sum(self.layer_fields(domain, grid_points, cutoff, progress), axis=0)
        lo, hi = total.min(), total.max()
        return (total - lo) / (hi - lo) if hi > lo else np.zeros_like(total)

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

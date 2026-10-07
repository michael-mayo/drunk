"""Multi-scale terrain: composites of drunks at several scales, combined into one height field."""

import math
from collections.abc import Callable

import numba
import numpy as np

from app.composite_drunk import CompositeDrunk


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

        sum_j w_j * L_j / std(L_j)

    then scaled linearly to [0, 1]. Scaling each layer to unit standard
    deviation makes the weights ``w_j`` alone set how much relief each scale
    contributes. Weights growing as ``s ** h`` make height differences grow
    with distance roughly as ``lag ** h``, like natural terrain.

    A layer at scale ``s`` is ``s / s_min`` times smoother than the finest, so
    it is evaluated on a grid that much coarser and resampled bilinearly,
    which keeps coarse layers as cheap as fine ones. The map wraps around:
    deposits leaving one edge re-enter at the opposite one, so the map tiles
    seamlessly. Drunks' homes should be sampled inside the map.
    """

    def __init__(
        self,
        layers: list[CompositeDrunk],
        scales: list[float],
        weights: list[float],
        references: list[CompositeDrunk | None] | None = None,
        finest_scale: float | None = None,
    ) -> None:
        """Combine ``layers`` (one composite per entry of ``scales``), each scaled to unit std and multiplied by its weight.

        ``references``, if given, holds for each layer an optional composite
        whose field's standard deviation scales the layer instead of the
        layer's own. A layer whose drunks were born on the paths of larger
        drunks (clustered on high ground, see ``app.pipeline``) passes the
        same drunks spread evenly here, so it is scaled like an even layer
        and its drunks keep their full relief where they gather. ``finest_scale`` sets the scale whose grid is the full
        resolution (default: the smallest of ``scales``), so a subset of a
        map's layers is evaluated on the same grids as the whole map.
        """
        if not layers or not len(layers) == len(scales) == len(weights):
            raise ValueError("LayeredDrunk needs one or more layers, each with a scale and a weight")
        if min(scales) <= 0:
            raise ValueError(f"layer scales must be positive, got {scales}")
        self.layers = list(layers)
        self.scales = [float(s) for s in scales]
        self.weights = [float(w) for w in weights]
        self.references = list(references) if references is not None else [None] * len(layers)
        if len(self.references) != len(self.layers):
            raise ValueError("LayeredDrunk needs one reference (or None) per layer")
        self.finest_scale = float(finest_scale) if finest_scale is not None else min(self.scales)

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
            if self.references[i] is not None:
                self.references[i].walk(num_steps)
            if progress is not None:
                progress((i + 1) / len(self.layers), f"walked layer {i + 1} of {len(self.layers)} (scale {scale:g})")

    def layer_grid_points(self, scale: float, grid_points: int) -> int:
        """Points per side of the coarser grid on which the layer at ``scale`` is evaluated."""
        return max(16, round(grid_points / (scale / self.finest_scale)))

    def layer_fields(
        self,
        domain: float,
        grid_points: int,
        cutoff: float,
        progress: Callable[[float, str], None] | None = None,
    ) -> list[np.ndarray]:
        """Each layer's weighted contribution to the height field, on a ``grid_points`` x ``grid_points`` wrap-around grid.

        Layer ``j`` is evaluated on its coarser grid, resampled, scaled to
        unit standard deviation (its reference's, if it has one) and
        multiplied by its weight; the
        height field is their sum. ``progress``, if given, is called after
        each layer with the fraction of layers done and a message.
        """
        fields = []
        for i, (layer, scale) in enumerate(zip(self.layers, self.scales)):
            n = self.layer_grid_points(scale, grid_points)
            coarse = layer.density(domain, n, cutoff)
            field = _resample_periodic(coarse, grid_points)
            reference = self.references[i]
            if reference is None:
                sd = field.std()
            else:
                sd = _resample_periodic(reference.density(domain, n, cutoff), grid_points).std()
            fields.append(self.weights[i] * field / sd if sd > 0 else np.zeros_like(field))
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
        once, starting at its lower-left corner and leaving out the far edges
        (the same lines as the near edges). ``progress`` is passed to
        ``layer_fields``.
        """
        total = np.sum(self.layer_fields(domain, grid_points, cutoff, progress), axis=0)
        lo, hi = total.min(), total.max()
        return (total - lo) / (hi - lo) if hi > lo else np.zeros_like(total)

    def __str__(self) -> str:
        """Summary of the layered field and of each layer."""
        lines = [f"LayeredDrunk(layers={len(self.layers)}, steps={self.num_steps})"]
        for scale, weight, layer in zip(self.scales, self.weights, self.layers):
            d = layer.drunks[0]
            lines.append(
                f"  scale={scale:g} weight={weight:g}: {len(layer.drunks)} drunks, step_size={d.step_size:g}, "
                f"r0={d.r0:g}, variance={d.variance:g}, kappa_max {min(x.kappa_max for x in layer.drunks):.4g}"
                f"-{max(x.kappa_max for x in layer.drunks):.4g}"
            )
        return "\n".join(lines)

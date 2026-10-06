"""A group of independent drunks that walk in parallel and render as one combined field."""

import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

from app.drunk import Drunk
from app.rendering import save_heatmap
from app.rendering import square_grid


def _run_steps(drunk: Drunk, n: int) -> Drunk:
    """Worker: advance ``drunk`` by ``n`` steps and return it (runs in a child process)."""
    drunk.steps(n)
    return drunk


def _normalise(field: np.ndarray) -> np.ndarray:
    """Scale a non-negative ``field`` by its maximum so it lies in [0, 1] (an all-zero field is returned unchanged)."""
    peak = field.max()
    return field / peak if peak > 0 else field


def _density(drunk: Drunk, gx: np.ndarray, gy: np.ndarray, cutoff: float) -> np.ndarray:
    """Worker: evaluate ``drunk``'s summed deposits on the grid ``gx`` x ``gy`` (runs in a child process)."""
    return drunk.density(gx, gy, cutoff)


class CompositeDrunk:
    """A composite of independent, already-constructed member drunks.

    The composite is a container rather than a kind of ``Drunk``: it holds
    the members it is given and offers the same walking and rendering
    operations (``step``, ``steps``, ``density``, ``to_png``), applied to all
    of them. Members may have different parameters. The composite has no
    randomness or deposits of its own: its location is the centroid of the
    members' locations, and its field is the sum of their fields, normalised
    by its maximum to lie in [0, 1].

    ``steps`` and ``to_png`` farm the members out to a process pool (threads
    wouldn't help, as the stepping loop is pure Python and holds the GIL).
    This is race-free by construction: each worker receives its own pickled
    copy of one member and returns a result, so no mutable state is shared.
    Results come back in submission order and are applied only in the parent
    process, so the output is identical to a sequential run.
    """

    def __init__(self, drunks: list[Drunk], max_workers: int | None = None) -> None:
        """Wrap ``drunks``, which must be non-empty and have all taken the same number of steps.

        The composite keeps references to the given ``Drunk`` objects, and
        updates them in place as it walks. ``max_workers`` caps the process
        pool size; ``None`` uses one worker per CPU (never more than the
        number of members).
        """
        if not drunks:
            raise ValueError("CompositeDrunk needs at least one drunk")
        # The centroid path averages members step by step, so their paths must align.
        if len({d.num_steps for d in drunks}) != 1:
            raise ValueError("All drunks in a CompositeDrunk must have taken the same number of steps")
        self.drunks = list(drunks)
        self.max_workers = max_workers

    @property
    def positions(self) -> list[tuple[float, float]]:
        """Centroid of the members' positions after each step, starting at (0, 0)."""
        centroid = np.mean([np.array(d.positions) for d in self.drunks], axis=0)
        return [tuple(p) for p in centroid]

    @property
    def location(self) -> tuple[float, float]:
        """Centroid of the members' current locations."""
        x, y = np.mean([d.location for d in self.drunks], axis=0)
        return float(x), float(y)

    @property
    def num_steps(self) -> int:
        """Number of steps each member has taken."""
        return self.drunks[0].num_steps

    def distance_from_origin(self) -> float:
        """Straight-line distance of the centroid from the starting point (0, 0)."""
        return math.hypot(*self.location)

    def _pool(self) -> ProcessPoolExecutor:
        """Create a process pool sized to the members and ``max_workers``."""
        workers = min(len(self.drunks), self.max_workers or os.process_cpu_count() or 1)
        return ProcessPoolExecutor(max_workers=workers)

    def step(self) -> tuple[float, float]:
        """Advance every member by one step and return the new centroid.

        Runs sequentially: one step is far cheaper than shipping a drunk to
        another process and back.
        """
        for drunk in self.drunks:
            drunk.step()
        return self.location

    def steps(self, n: int = 100) -> tuple[float, float]:
        """Advance every member by ``n`` steps in parallel and return the final centroid."""
        with self._pool() as pool:
            advanced = list(pool.map(_run_steps, self.drunks, [n] * len(self.drunks)))
        # Workers advanced copies; copy their state (deposits and RNG) back onto
        # the original objects so callers' references to the members stay current.
        for drunk, result in zip(self.drunks, advanced):
            vars(drunk).update(vars(result))
        return self.location

    def density(self, gx: np.ndarray, gy: np.ndarray, cutoff: float = 1 / 4096) -> np.ndarray:
        """Sum of all members' deposits on the grid ``gx`` x ``gy``, normalised to [0, 1].

        The sum is divided by its maximum over this grid, so the peak is 1.
        Returns shape ``(len(gy), len(gx))``.
        """
        return _normalise(np.sum([d.density(gx, gy, cutoff) for d in self.drunks], axis=0))

    def to_png(self, filename: Path | str, grid_points: int = 400, cutoff: float = 1 / 4096) -> None:
        """Save a heatmap of the sum of all members' deposits, normalised to [0, 1].

        Each member's field is evaluated in parallel on a shared grid that
        covers all members' paths plus a margin of three standard deviations
        of the widest member's deposits. Deposits are truncated below
        ``cutoff`` times their peak.
        """
        paths = [np.array(d.positions) for d in self.drunks]
        margin = 3.0 * math.sqrt(max(d.variance for d in self.drunks))
        gx, gy = square_grid(np.vstack(paths), margin, grid_points)
        with self._pool() as pool:
            n = len(self.drunks)
            fields = list(pool.map(_density, self.drunks, [gx] * n, [gy] * n, [cutoff] * n))
        # Summed in member order in the parent, so the result is deterministic.
        field = _normalise(np.sum(fields, axis=0))
        title = (
            f"CompositeDrunk of {len(self.drunks)}: {self.num_steps} steps each, "
            f"centroid distance {self.distance_from_origin():.2f}"
        )
        save_heatmap(filename, field, gx, gy, title, label="normalised summed deposit amplitude")

    def __str__(self) -> str:
        """Summary of the composite followed by one line per member."""
        x, y = self.location
        header = (
            f"CompositeDrunk(n={len(self.drunks)}, steps={self.num_steps}, "
            f"centroid=({x:.2f}, {y:.2f}), centroid_distance={self.distance_from_origin():.2f})"
        )
        return "\n".join([header] + [f"  {d}" for d in self.drunks])

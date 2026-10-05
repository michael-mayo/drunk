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


def _density(drunk: Drunk, gx: np.ndarray, gy: np.ndarray, cutoff: float) -> np.ndarray:
    """Worker: evaluate ``drunk``'s summed deposits on the grid ``gx`` x ``gy`` (runs in a child process)."""
    return drunk.density(gx, gy, cutoff)


class CompositeDrunk(Drunk):
    """A composite of ``n`` independent member drunks, one per seed.

    The composite takes the same walk and deposit parameters as ``Drunk`` and
    passes them to every member. It has no randomness or deposits of its own:
    its location is the centroid of the members' locations, and its rendered
    field is the sum of the members' fields.

    ``steps`` and ``to_png`` farm the members out to a process pool (threads
    wouldn't help, as the stepping loop is pure Python and holds the GIL).
    This is race-free by construction: each worker receives its own pickled
    copy of one member and returns a result, so no mutable state is shared.
    Results come back in submission order and are combined only in the parent
    process, so the output is identical to a sequential run.
    """

    def __init__(
        self,
        seeds: list[int],
        step_size: float = 1.0,
        kappa_max: float = 2.0,
        r0: float = 10.0,
        variance: float = 1.0,
        decay: float = 0.999,
        initial_amplitude: float = 1.0,
        max_workers: int | None = None,
    ) -> None:
        """Create one member ``Drunk`` per seed, all sharing the given parameters.

        ``max_workers`` caps the process pool size; ``None`` uses one worker
        per CPU (never more than the number of members).
        """
        if not seeds:
            raise ValueError("CompositeDrunk needs at least one seed")
        params = dict(
            step_size=step_size,
            kappa_max=kappa_max,
            r0=r0,
            variance=variance,
            decay=decay,
            initial_amplitude=initial_amplitude,
        )
        # Initialise the shared parameters; the base RNG and deposit list go unused.
        super().__init__(seeds[0], **params)
        self.seeds = list(seeds)
        self.max_workers = max_workers
        self.drunks = [Drunk(seed, **params) for seed in self.seeds]

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
            # map() preserves input order; members are replaced by the advanced
            # copies returned from the workers, only after all have finished.
            self.drunks = list(pool.map(_run_steps, self.drunks, [n] * len(self.drunks)))
        return self.location

    def density(self, gx: np.ndarray, gy: np.ndarray, cutoff: float = 1 / 4096) -> np.ndarray:
        """Sum of all members' deposits on the regular grid ``gx`` x ``gy``, shape ``(len(gy), len(gx))``."""
        return np.sum([d.density(gx, gy, cutoff) for d in self.drunks], axis=0)

    def to_png(self, filename: Path | str, grid_points: int = 400, cutoff: float = 1 / 4096) -> None:
        """Save a heatmap of the sum of all members' deposits, with every member's path overlaid.

        Each member's field is evaluated in parallel on a shared grid that
        covers all members' paths plus a three-standard-deviation margin.
        Deposits are truncated below ``cutoff`` times their peak.
        """
        paths = [np.array(d.positions) for d in self.drunks]
        gx, gy = square_grid(np.vstack(paths), 3.0 * math.sqrt(self.variance), grid_points)
        with self._pool() as pool:
            n = len(self.drunks)
            fields = list(pool.map(_density, self.drunks, [gx] * n, [gy] * n, [cutoff] * n))
        # Summed in member order in the parent, so the result is deterministic.
        field = np.sum(fields, axis=0)
        title = (
            f"CompositeDrunk of {len(self.drunks)}: {self.num_steps} steps each, "
            f"centroid distance {self.distance_from_origin():.2f}"
        )
        save_heatmap(filename, field, gx, gy, paths, title)

    def __str__(self) -> str:
        """Summary of the composite followed by one line per member."""
        x, y = self.location
        header = (
            f"CompositeDrunk(n={len(self.drunks)}, step_size={self.step_size}, kappa_max={self.kappa_max}, "
            f"r0={self.r0}, variance={self.variance}, decay={self.decay}, steps={self.num_steps}, "
            f"centroid=({x:.2f}, {y:.2f}), centroid_distance={self.distance_from_origin():.2f})"
        )
        return "\n".join([header] + [f"  {d}" for d in self.drunks])


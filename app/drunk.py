"""A random walker ("drunk") on the 2D plane that leaves a trail of Gaussian deposits."""

import math
from pathlib import Path

import numpy as np

from app.gaussian import GaussianDeposit
from app.rendering import save_heatmap
from app.rendering import square_grid


class Drunk:
    """A drunk who staggers a fixed distance in a random direction on each step.

    Step directions follow a von Mises distribution centred on the direction
    back to the origin. Its concentration grows with distance ``r`` as
    ``kappa = kappa_max * (1 - exp(-r / r0))``: near (0, 0) directions are
    close to uniform, while far away the drunk is increasingly biased towards
    home. ``kappa_max = 0`` gives a plain uniform random walk.

    After every step the drunk deposits a ``GaussianDeposit`` at its new
    location. Each deposit has uniformly random axis directions, variance
    ``variance`` along its major axis and ``variance * u`` (``u`` uniform in
    (0, 1]) along its minor axis. The k-th deposit (k = 0, 1, ...) has peak
    amplitude ``initial_amplitude * decay**k``, so later deposits are weaker.
    The drunk's path is the sequence of deposit centres, starting from (0, 0),
    and ``to_png`` renders the sum of all deposits.

    All randomness comes from a private RNG seeded at construction, making
    each walk reproducible from its seed.
    """

    def __init__(
        self,
        seed: int,
        step_size: float = 1.0,
        kappa_max: float = 2.0,
        r0: float = 10.0,
        variance: float = 1.0,
        decay: float = 0.999,
        initial_amplitude: float = 1.0,
    ) -> None:
        """Create a drunk at (0, 0) with its own RNG seeded from ``seed``.

        ``kappa_max`` is the strongest homeward bias (reached far from the
        origin) and ``r0`` is the distance over which the bias builds up.
        ``variance``, ``decay`` and ``initial_amplitude`` control the Gaussian
        deposited after each step.
        """
        self.seed = seed
        self.step_size = step_size
        self.kappa_max = kappa_max
        self.r0 = r0
        self.variance = variance
        self.decay = decay
        self.initial_amplitude = initial_amplitude
        self._rng = np.random.default_rng(seed)
        self.deposits: list[GaussianDeposit] = []

    @property
    def positions(self) -> list[tuple[float, float]]:
        """Every location visited, starting at (0, 0) and followed by each deposit centre."""
        return [(0.0, 0.0)] + [d.center for d in self.deposits]

    @property
    def location(self) -> tuple[float, float]:
        """Current (x, y) location."""
        return self.deposits[-1].center if self.deposits else (0.0, 0.0)

    @property
    def num_steps(self) -> int:
        """Number of steps taken so far."""
        return len(self.deposits)

    def distance_from_origin(self) -> float:
        """Straight-line distance from the starting point (0, 0)."""
        return math.hypot(*self.location)

    def kappa(self) -> float:
        """Von Mises concentration (homeward bias strength) at the current location."""
        return self.kappa_max * (1.0 - math.exp(-self.distance_from_origin() / self.r0))

    def step(self) -> tuple[float, float]:
        """Move ``step_size`` towards a biased direction, deposit a Gaussian, and return the new location.

        The direction is drawn from a von Mises distribution centred on the
        bearing back to (0, 0) with concentration ``kappa()``. At the origin
        ``kappa()`` is 0, so the undefined bearing has no effect.
        """
        x, y = self.location
        angle = self._rng.vonmises(math.atan2(-y, -x), self.kappa())
        new_location = (x + self.step_size * math.cos(angle), y + self.step_size * math.sin(angle))
        self.deposits.append(self._make_deposit(new_location))
        return new_location

    def steps(self, n: int = 100) -> tuple[float, float]:
        """Take ``n`` steps and return the final location."""
        for _ in range(n):
            self.step()
        return self.location

    def _make_deposit(self, center: tuple[float, float]) -> GaussianDeposit:
        """Build the next deposit at ``center`` with random orientation and aspect ratio."""
        # Axes are symmetric under a half-turn, so [0, pi) covers every orientation.
        axis_angle = self._rng.uniform(0.0, math.pi)
        # 1 - random() lies in (0, 1], so the minor variance is never zero.
        aspect = 1.0 - self._rng.random()
        return GaussianDeposit(
            center=center,
            angle=axis_angle,
            var_major=self.variance,
            var_minor=self.variance * aspect,
            amplitude=self.initial_amplitude * self.decay ** len(self.deposits),
        )

    def density(self, gx: np.ndarray, gy: np.ndarray, cutoff: float = 1 / 4096) -> np.ndarray:
        """Sum of all deposits on the regular grid ``gx`` x ``gy``, shape ``(len(gy), len(gx))``.

        Each deposit is only evaluated where it exceeds ``cutoff`` times its
        peak (see ``GaussianDeposit.add_to_grid``); ``cutoff <= 0`` is exact.
        """
        field = np.zeros((len(gy), len(gx)))
        for deposit in self.deposits:
            deposit.add_to_grid(field, gx, gy, cutoff)
        return field

    def to_png(self, filename: Path | str, grid_points: int = 400, cutoff: float = 1 / 4096) -> None:
        """Save a heatmap of the summed deposits to ``filename``, with the path overlaid faintly.

        The plotted area covers every visited location plus a margin of three
        standard deviations, sampled on a ``grid_points`` x ``grid_points``
        grid. Deposits are truncated below ``cutoff`` times their peak.
        Parent folders are created if needed.
        """
        path = np.array(self.positions)
        gx, gy = square_grid(path, 3.0 * math.sqrt(self.variance), grid_points)
        field = self.density(gx, gy, cutoff)
        title = f"Drunk seed={self.seed}: {self.num_steps} deposits, distance {self.distance_from_origin():.2f}"
        save_heatmap(filename, field, gx, gy, [path], title)

    def __str__(self) -> str:
        """Human-readable summary of the drunk's state."""
        x, y = self.location
        last_amplitude = self.deposits[-1].amplitude if self.deposits else self.initial_amplitude
        return (
            f"Drunk(seed={self.seed}, step_size={self.step_size}, kappa_max={self.kappa_max}, "
            f"r0={self.r0}, variance={self.variance}, decay={self.decay}, steps={self.num_steps}, "
            f"location=({x:.2f}, {y:.2f}), distance={self.distance_from_origin():.2f}, "
            f"last_amplitude={last_amplitude:.3f})"
        )

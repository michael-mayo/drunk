"""Random walkers ("drunks") on the 2D plane that leave a trail of Gaussian deposits."""

import math
from dataclasses import dataclass

import numba
import numpy as np

from app.deposits import Deposits


@dataclass(frozen=True)
class Drunk:
    """One drunk's parameters: who it is, where it starts, how it walks and what it deposits.

    The drunk starts at ``home`` and staggers ``step_size`` in a random
    direction on each step, biased back towards home: directions follow a
    von Mises distribution centred on the bearing to ``home``, whose
    concentration grows with distance ``r`` from home as
    ``kappa = kappa_max * (1 - exp(-r / r0))``. Near home directions are
    close to uniform, while far away the drunk is increasingly drawn back.
    ``kappa_max = 0`` gives a plain uniform random walk.

    After every step the drunk deposits a Gaussian at its new location, with
    uniformly random axis directions, variance ``variance`` along its major
    axis and ``variance * u`` (``u`` uniform in (0, 1]) along its minor axis.
    The k-th deposit (k = 0, 1, ...) has peak amplitude
    ``initial_amplitude * decay**k``, so later deposits are weaker.

    All randomness comes from ``seed``, so each walk is reproducible.
    Walking is done for many drunks at once by ``walk_drunks``.
    """

    seed: int
    step_size: float = 1.0
    kappa_max: float = 2.0
    r0: float = 10.0
    variance: float = 1.0
    decay: float = 0.999
    initial_amplitude: float = 1.0
    home: tuple[float, float] = (0.0, 0.0)

    def __post_init__(self) -> None:
        """Check the seed fits numba's random generator, which takes 32-bit seeds."""
        if not 0 <= self.seed < 2**32:
            raise ValueError(f"a drunk's seed must be in [0, 2**32), got {self.seed}")


@numba.njit(parallel=True, cache=True)
def _walk(
    seeds: np.ndarray,
    home_x: np.ndarray,
    home_y: np.ndarray,
    step_size: np.ndarray,
    kappa_max: np.ndarray,
    r0: np.ndarray,
    variance: np.ndarray,
    decay: np.ndarray,
    initial_amplitude: np.ndarray,
    num_steps: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Walk every drunk ``num_steps`` steps; returns deposit arrays of shape ``(drunks, num_steps)``.

    Drunks are walked in parallel. Each one reseeds the generator of the
    thread walking it and then draws all its numbers on that thread, so its
    walk depends only on its own seed.
    """
    n = seeds.shape[0]
    xs = np.empty((n, num_steps))
    ys = np.empty((n, num_steps))
    angle = np.empty((n, num_steps))
    var_major = np.empty((n, num_steps))
    var_minor = np.empty((n, num_steps))
    amplitude = np.empty((n, num_steps))
    for j in numba.prange(n):
        np.random.seed(seeds[j])
        x = home_x[j]
        y = home_y[j]
        for k in range(num_steps):
            r = math.hypot(x - home_x[j], y - home_y[j])
            kappa = kappa_max[j] * (1.0 - math.exp(-r / r0[j]))
            # At home kappa is 0, so the undefined bearing has no effect.
            heading = np.random.vonmises(math.atan2(home_y[j] - y, home_x[j] - x), kappa)
            x += step_size[j] * math.cos(heading)
            y += step_size[j] * math.sin(heading)
            xs[j, k] = x
            ys[j, k] = y
            # Axes are symmetric under a half-turn, so [0, pi) covers every orientation.
            angle[j, k] = np.random.uniform(0.0, math.pi)
            var_major[j, k] = variance[j]
            # 1 - random() lies in (0, 1], so the minor variance is never zero.
            var_minor[j, k] = variance[j] * (1.0 - np.random.random())
            amplitude[j, k] = initial_amplitude[j] * decay[j] ** k
    return xs, ys, angle, var_major, var_minor, amplitude


def walk_drunks(drunks: list[Drunk], num_steps: int) -> Deposits:
    """Walk each drunk ``num_steps`` steps from its home and return all their deposits.

    Deposits are ordered drunk by drunk, each drunk's in step order, so drunk
    ``j``'s path is entries ``j * num_steps`` to ``(j + 1) * num_steps - 1``
    of ``x`` and ``y`` (preceded by its ``home``).
    """
    def column(name: str, dtype: type = np.float64) -> np.ndarray:
        """One parameter of every drunk, as an array."""
        return np.array([getattr(d, name) for d in drunks], dtype=dtype)

    homes = np.array([d.home for d in drunks], dtype=np.float64).reshape(len(drunks), 2)
    arrays = _walk(
        column("seed", np.int64), homes[:, 0].copy(), homes[:, 1].copy(), column("step_size"),
        column("kappa_max"), column("r0"), column("variance"), column("decay"), column("initial_amplitude"),
        int(num_steps),
    )
    return Deposits(*(a.ravel() for a in arrays))

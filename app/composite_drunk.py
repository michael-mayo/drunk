"""A group of independent drunks that walk together and render as one combined field."""

import numpy as np

from app.deposits import Deposits
from app.deposits import deposit_field
from app.drunk import Drunk
from app.drunk import walk_drunks


class CompositeDrunk:
    """A composite of independent member drunks, whose field is the sum of all their deposits.

    The composite is a container rather than a kind of ``Drunk``: it holds
    the members it is given (which may have different parameters), walks
    them all at once in compiled parallel code (``walk``), and keeps their
    deposits for ``density``. It has no randomness of its own.
    """

    def __init__(self, drunks: list[Drunk]) -> None:
        """Wrap ``drunks``, which must be non-empty; none has walked yet."""
        if not drunks:
            raise ValueError("CompositeDrunk needs at least one drunk")
        self.drunks = list(drunks)
        self.num_steps = 0
        self.deposits: Deposits | None = None

    def walk(self, num_steps: int) -> None:
        """Walk every member ``num_steps`` steps from its home, replacing any earlier walk."""
        self.deposits = walk_drunks(self.drunks, num_steps)
        self.num_steps = num_steps

    def density(self, domain: float, grid_points: int, cutoff: float) -> np.ndarray:
        """Sum of all members' deposits on a ``grid_points`` x ``grid_points`` wrap-around grid.

        The grid covers the square of side ``domain`` centred on the origin
        once (see ``periodic_axis``), and deposits are wrapped into it.
        Gaussians are truncated below ``cutoff`` times their peak.
        """
        if self.deposits is None:
            raise RuntimeError("walk the composite before evaluating its density")
        return deposit_field(self.deposits, -domain / 2.0, domain / grid_points, grid_points, cutoff, periodic=True)

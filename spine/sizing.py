"""
How large a position may be, and why.

§11.2: "Position limits derive from **monetary loss scenarios, concentration,
liquidity and uncertainty** — not from `n_eff`. v1 proposed sizing on cluster
effective sample size, which conflates statistical evidence with financial risk."

That was stated and never implemented. `decide()` takes `max_notional_usd` and
`cluster_exposure_cap_usd` as arguments and every caller so far has passed a
number somebody made up — which means the design's sizing rule has been a
paragraph, and the tests have been checking that arbitrary limits are enforced
rather than that correct limits are computed.

The four inputs, and what each one is doing:

**Loss budget.** A binary contract's worst case is total: YES bought at 61c
returns nothing if the event does not occur. So the first bound is simply the
loss you are willing to take on one position — not a fraction of capital scaled
by confidence, because confidence is what the estimate is *for*, and letting it
also set the size counts the same belief twice.

**Liquidity.** A position you cannot exit is larger than it looks. The bound is
a share of the depth actually resting within a stated distance of the touch, not
of "volume", which includes trades you were not part of and cannot undo.

**Uncertainty.** A wider interval means a less resolved estimate, so the size
is discounted by interval width. This is the one place uncertainty touches
sizing, it is a *reduction* only, and it is bounded — a narrow interval never
inflates a position above the loss budget. §5.1's separation is the reason:
estimate, uncertainty and permission are three things, and uncertainty belongs
to permission, never to the estimate.

**Concentration.** Eight markets downstream of one thesis are one bet. The cap
is driven by **loss covariance** — `forecast_edges` with `dependence_kind='loss'`
— and explicitly not by score correlation, which is a different quantity that
v1 conflated. Two forecasts can be scored independently and lose together.

`n_eff` appears nowhere in this module, and a test asserts it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .shadow import Book


class SizingError(RuntimeError):
    """A size as requested would not mean what it claims."""


# How far from the touch depth still counts as exitable. Beyond this the book is
# not liquidity you can leave through in a hurry, it is liquidity you would move.
EXIT_BAND_BP = 300

# The share of exitable depth a single position may take. Taking all of it means
# being the entire other side on the way out.
DEPTH_SHARE = 0.25


@dataclass(frozen=True)
class Size:
    """A position limit and the binding reason for it."""
    max_notional_usd: float
    loss_budget_usd: float
    liquidity_bound_usd: float
    uncertainty_discount: float
    binding: str                    # which bound actually decided it
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"${self.max_notional_usd:.2f} (loss ${self.loss_budget_usd:.2f}, "
                f"liquidity ${self.liquidity_bound_usd:.2f}, "
                f"uncertainty x{self.uncertainty_discount:.2f}) "
                f"— bound by {self.binding}")


def exitable_depth_usd(book: Book, side: str, band_bp: int = EXIT_BAND_BP) -> float:
    """
    Depth you could plausibly leave through, on the side you would exit into.

    A YES position is closed by selling into the **bids**, so the liquidity that
    matters is not the one you bought from. Sizing against the entry side is a
    standard way to build a position that is comfortable to open and impossible
    to close.
    """
    if side == "YES":
        levels, touch = book.bids, book.best_bid_bp
        if touch is None:
            return 0.0
        return sum(l.size_usd for l in levels if l.price_bp >= touch - band_bp)
    if side == "NO":
        # Closing a NO position means buying YES back, which lifts the asks.
        levels, touch = book.asks, book.best_ask_bp
        if touch is None:
            return 0.0
        return sum(l.size_usd for l in levels if l.price_bp <= touch + band_bp)
    raise SizingError(f"side must be YES or NO, got {side!r}")


def uncertainty_discount(p_lo_bp: int, p_hi_bp: int,
                         reference_width_bp: int = 2000) -> float:
    """
    Size multiplier from interval width. In [0, 1], and never above 1.

    A width at or below `reference_width_bp` is undiscounted; wider intervals
    shrink the position linearly and a maximally wide interval goes to zero.
    The asymmetry is deliberate: a narrow interval does not earn a larger
    position, because that would let confidence set size and the loss budget
    already did.
    """
    width = p_hi_bp - p_lo_bp
    if width < 0:
        raise SizingError("an interval cannot have negative width")
    if width <= reference_width_bp:
        return 1.0
    span = 10000 - reference_width_bp
    return max(0.0, 1.0 - (width - reference_width_bp) / span)


def position_limit(
    *,
    loss_budget_usd: float,
    book: Book,
    side: str,
    p_lo_bp: int,
    p_hi_bp: int,
    band_bp: int = EXIT_BAND_BP,
    depth_share: float = DEPTH_SHARE,
    reference_width_bp: int = 2000,
) -> Size:
    """
    The largest position this contract supports, and which bound decided it.

    Reporting the binding constraint matters more than the number: "bound by
    liquidity" and "bound by loss budget" call for different responses, and a
    bare figure hides which one you are in.
    """
    if loss_budget_usd <= 0:
        raise SizingError("the loss budget must be positive")
    if not 0 < depth_share <= 1:
        raise SizingError("depth_share must be in (0, 1]")

    notes = []
    depth = exitable_depth_usd(book, side, band_bp)
    liquidity = depth * depth_share
    if depth == 0.0:
        notes.append(
            f"no depth within {band_bp}bp of the touch on the exit side; a "
            "position here could be opened and not closed")

    discount = uncertainty_discount(p_lo_bp, p_hi_bp, reference_width_bp)
    if discount < 1.0:
        notes.append(
            f"interval is {p_hi_bp - p_lo_bp}bp wide against a "
            f"{reference_width_bp}bp reference; size cut to {discount:.0%}")

    limit = min(loss_budget_usd, liquidity) * discount
    binding = ("liquidity" if liquidity < loss_budget_usd else "loss budget")
    if discount < 1.0 and limit < min(loss_budget_usd, liquidity):
        binding += " then uncertainty"

    return Size(max_notional_usd=limit, loss_budget_usd=loss_budget_usd,
                liquidity_bound_usd=liquidity, uncertainty_discount=discount,
                binding=binding, notes=notes)


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------

def loss_linked(con: sqlite3.Connection, forecast_hash: str) -> list[str]:
    """
    Forecasts that lose money together with this one.

    Reads the **loss** graph, never the score graph. §11.2 is explicit that they
    are different quantities and v1 conflated them: two positions can be scored
    independently and still be one bet, which is the case concentration limits
    exist for.
    """
    cur = con.execute(
        """WITH RECURSIVE reach(n) AS (
               SELECT child_hash FROM forecast_edges
               WHERE parent_hash = ? AND dependence_kind = 'loss'
               UNION
               SELECT e.child_hash FROM forecast_edges e
               JOIN reach r ON e.parent_hash = r.n
               WHERE e.dependence_kind = 'loss'
           ) SELECT n FROM reach ORDER BY n""", (forecast_hash,))
    return [r[0] for r in cur.fetchall()]


def cluster_exposure_used_usd(con: sqlite3.Connection,
                              forecast_hash: str) -> float:
    """
    Notional already committed across everything that loses with this forecast.

    Counts *permitted* decisions only, in either mode: a paper run that did not
    track its own simulated exposure would produce concentration numbers no
    live run could reproduce, which defeats the point of running it first.
    """
    linked = loss_linked(con, forecast_hash)
    if not linked:
        return 0.0
    placeholders = ",".join("?" * len(linked))
    row = con.execute(
        f"""SELECT COALESCE(SUM(d.intended_size_usd), 0.0)
            FROM trade_decisions d
            JOIN forecasts f ON f.id = d.forecast_id
            WHERE d.permitted = 1 AND f.forecast_hash IN ({placeholders})""",
        linked).fetchone()
    return float(row[0])


def concentration_cap_usd(loss_budget_usd: float, n_linked: int,
                          correlation: float = 1.0) -> float:
    """
    A cap on the whole correlated group, not on each member.

    At correlation 1.0 the group is one position and gets one budget — which is
    the case §11.2 cares about, since eight markets downstream of one thesis
    lose together in full. Lower correlation relaxes it toward the sum, but
    never past it.
    """
    if loss_budget_usd <= 0:
        raise SizingError("the loss budget must be positive")
    if n_linked < 0:
        raise SizingError("a group cannot have negative membership")
    if not -1.0 <= correlation <= 1.0:
        raise SizingError("correlation must be in [-1, 1]")
    members = n_linked + 1
    # Interpolate between one shared budget (fully correlated) and a budget per
    # member (independent). Negative correlation is treated as independent:
    # positions that hedge each other are not a concentration problem, and
    # crediting them with one would be sizing on a correlation estimate.
    r = max(0.0, correlation)
    return loss_budget_usd * (1.0 + (1.0 - r) * (members - 1))

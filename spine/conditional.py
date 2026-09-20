"""
Conditional probability, and the one adjustment that is openly a guess.

Two §5 corrections that had no code, and they are opposites: the first is
arithmetic that v1 got wrong, the second is arithmetic that was never justified
at all.

**§5.4 — the law of total probability.** v1 wrote `P(B) = P(B|A)·P(A)`, which is
valid only when B cannot occur without A. In general the second term is there:

    P(B) = P(B|A)·P(A) + P(B|¬A)·P(¬A)

Dropping it does not introduce noise, it introduces a **bias in one direction**:
every such probability comes out too low, by exactly the mass of the branch that
was ignored. The failure mode is quiet, because the answer stays in [0,1] and
looks like a probability. So `marginal()` requires `p_b_given_not_a` as a
positional argument with no default. Defaulting it to zero would make the v1
error the easy path, and an argument you have to supply is one you have to think
about — including thinking "it really is zero here", which `exclusive()` is for.

**§5.5 — the coordination penalty.** `p ← p·ρ^(N−1)` with ρ ≈ 0.7 "does not
follow from having done an incentive analysis. It is a made-up functional form
with a made-up constant." v2 retains it *only* as an explicitly labelled
subjective assumption with mandatory sensitivity testing.

That condition is enforceable, so it is enforced: `coordination_penalty()`
returns a band over a range of ρ and never a bare number, and the return type
has no attribute that gives you a single adjusted probability without also
handing you the spread. If the spread is wide enough to change the decision, the
result says so — which is the whole content of "mandatory sensitivity testing"
and the only honest use of a made-up constant.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


class ConditionalError(ValueError):
    """A probability computation would be wrong, or wrong in a hidden way."""


def _check(p: float, name: str) -> float:
    if not isinstance(p, (int, float)) or p != p:
        raise ConditionalError(f"{name} is not a number")
    if not 0.0 <= p <= 1.0:
        raise ConditionalError(f"{name}={p} is not a probability")
    return float(p)


def marginal(p_b_given_a: float, p_a: float, p_b_given_not_a: float) -> float:
    """
    P(B) from both branches. §5.4.

    `p_b_given_not_a` has no default on purpose. v1's error was to omit the
    branch where A does not hold, which biases every result downward by exactly
    the mass of what was dropped — and the number still looks like a probability
    afterwards, which is what made it survive. Supplying the argument is the
    point at which someone has to decide whether B really cannot happen without
    A; `exclusive()` says that explicitly when it is true.
    """
    p_b_a = _check(p_b_given_a, "p_b_given_a")
    pa = _check(p_a, "p_a")
    p_b_na = _check(p_b_given_not_a, "p_b_given_not_a")
    return p_b_a * pa + p_b_na * (1.0 - pa)


def exclusive(p_b_given_a: float, p_a: float) -> float:
    """
    P(B) where B genuinely cannot occur without A.

    The same arithmetic v1 used, available under a name that states the
    assumption it requires. A call to this function is a claim; a call to
    `marginal` with a zero third argument is the same claim written where nobody
    will read it.
    """
    return marginal(p_b_given_a, p_a, 0.0)


def conditional_from_marginals(p_b: float, p_a: float,
                               p_b_given_not_a: float) -> float:
    """
    Recover P(B|A) from the marginal. Refuses when A is certain or impossible.

    At `p_a` of 0 or 1 the conditional is not identified — there is no evidence
    about the other branch — and returning anything would invent a number.
    """
    pb = _check(p_b, "p_b")
    pa = _check(p_a, "p_a")
    p_b_na = _check(p_b_given_not_a, "p_b_given_not_a")
    if pa <= 0.0 or pa >= 1.0:
        raise ConditionalError(
            f"P(A)={pa} leaves P(B|A) unidentified: with A certain or "
            "impossible the data say nothing about the other branch")
    value = (pb - p_b_na * (1.0 - pa)) / pa
    if not 0.0 <= value <= 1.0:
        raise ConditionalError(
            f"the implied P(B|A)={value:.4f} is not a probability, so the three "
            "inputs are mutually inconsistent rather than merely uncertain")
    return value


def marginal_over_edges(
    con: sqlite3.Connection,
    parent_hash: str,
    p_parent: float,
    p_child_given_not_parent: float,
    dependence_kind: str = "evidence",
) -> dict[str, float]:
    """
    Marginals for every child of a forecast, using stored conditionals.

    `forecast_edges.conditional_prob` holds P(child | parent) and nothing had
    ever read it. The complement is required here for the same reason as above;
    it is a single value because a per-child complement is a modelling choice
    this function should not make silently on the caller's behalf.
    """
    rows = con.execute(
        "SELECT child_hash, conditional_prob FROM forecast_edges "
        "WHERE parent_hash=? AND dependence_kind=? ORDER BY child_hash",
        (parent_hash, dependence_kind)).fetchall()
    return {child: marginal(cond, p_parent, p_child_given_not_parent)
            for child, cond in rows}


# ---------------------------------------------------------------------------
# §5.5
# ---------------------------------------------------------------------------

# The v1 value, retained only as the centre of a range. Not a measurement, and
# the type system here will not let it be used as one.
RHO_CENTRE = 0.70
RHO_RANGE = (0.50, 0.90)


@dataclass(frozen=True)
class CoordinationBand:
    """
    A coordination-adjusted probability, with the spread that comes with it.

    There is deliberately no attribute giving a single adjusted probability
    without the band. §5.5 permits `p·ρ^(N−1)` only as a labelled subjective
    assumption *with mandatory sensitivity testing*, and a type that can hand
    back a point estimate makes the sensitivity optional in practice however
    firmly the documentation asks for it.
    """
    base_p: float
    n_actors: int
    rho_centre: float
    rho_low: float
    rho_high: float
    p_centre: float
    p_low: float
    p_high: float

    @property
    def spread(self) -> float:
        """Absolute width. Peaks at a middling actor count and then misleads."""
        return self.p_high - self.p_low

    @property
    def spread_ratio(self) -> float:
        """
        How many times larger the optimistic end is than the pessimistic one.

        This, not `spread`, is how much the guess is carrying. The absolute
        width peaks around four actors and *narrows* beyond it, because both
        ends collapse toward zero — so a wide-actor-count estimate can look
        tightly bounded while the choice of rho moves it by a factor of two
        hundred. Measured on 0.6: 1.8x at two actors, 5.8x at four, 198x at ten.
        """
        return self.p_high / self.p_low if self.p_low > 0 else float("inf")

    def decision_sensitive(self, threshold: float) -> bool:
        """Whether the choice of ρ moves the answer across a threshold."""
        return self.p_low < threshold <= self.p_high

    def summary(self) -> str:
        return (f"{self.base_p:.4f} with {self.n_actors} actors -> "
                f"{self.p_centre:.4f} [{self.p_low:.4f}, {self.p_high:.4f}] "
                f"= {self.spread_ratio:.1f}x over rho "
                f"{self.rho_low}-{self.rho_high} "
                f"(SUBJECTIVE: made-up form, made-up constant)")


def coordination_penalty(
    p: float,
    n_actors: int,
    rho_centre: float = RHO_CENTRE,
    rho_range: tuple[float, float] = RHO_RANGE,
) -> CoordinationBand:
    """
    Apply `p·ρ^(N−1)`, and return the band rather than a number. §5.5.

    Every caller gets the sensitivity whether or not it wanted it, because the
    adjustment is a guess with a plausible shape and the only defensible use of
    such a thing is to know how much it is carrying. If the band straddles the
    decision threshold, the coordination assumption is what made the decision —
    which `decision_sensitive()` answers directly.
    """
    base = _check(p, "p")
    if n_actors < 1:
        raise ConditionalError("a coordination penalty needs at least one actor")
    lo, hi = rho_range
    for r, name in ((rho_centre, "rho_centre"), (lo, "rho_low"), (hi, "rho_high")):
        if not 0.0 < r <= 1.0:
            raise ConditionalError(f"{name}={r} must be in (0, 1]")
    if lo > hi:
        raise ConditionalError("rho_range is inverted")
    if not lo <= rho_centre <= hi:
        raise ConditionalError(
            f"rho_centre={rho_centre} sits outside its own sensitivity range "
            f"{rho_range}; the band would not contain the estimate")

    exponent = n_actors - 1
    return CoordinationBand(
        base_p=base, n_actors=n_actors, rho_centre=rho_centre,
        rho_low=lo, rho_high=hi,
        p_centre=base * rho_centre ** exponent,
        p_low=base * lo ** exponent,
        p_high=base * hi ** exponent)

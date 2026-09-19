"""
Decision layer: executable price, conservative EV, abstention.

v2 §10.2. Three things this module refuses to do, each of which makes a backtest
look better than reality:

  * **Price at the midpoint.** A midpoint is not obtainable. Expected acquisition
    price comes from walking the actual book to the intended size.
  * **Use the point estimate for EV.** EV is computed against the conservative
    end of the uncertainty interval, so a wide interval reduces the trade rather
    than being decoration attached to it.
  * **Assume a fill.** Touching a limit is not being filled; passive orders carry
    queue risk and fill disproportionately when the price is moving against you.

The most common legitimate output is no trade, and every abstention records why.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A position is never permitted while eligibility is anything but tradeable.
# Close-only means existing positions can be exited and no new ones opened.
TRADEABLE = "tradeable"

# Modes. Under the paper-only posture (design section 2.1) the venue is
# close-only from our jurisdiction, so a live-mode eligibility check would
# abstain on every market and the decision layer would measure nothing. Paper
# mode computes the counterfactual instead — what the decision WOULD have been
# had the trade been permissible — which is what answers P3 as a research
# question. It records that it did so, so a simulated decision can never be
# mistaken for a permission to trade.
PAPER = "paper"
LIVE = "live"


class DecisionError(RuntimeError):
    """The decision could not be formed from the inputs given."""


@dataclass(frozen=True)
class BookLevel:
    price_bp: int     # basis points of $1, i.e. 6100 = 61c
    size_usd: float


@dataclass(frozen=True)
class Fill:
    """What walking the book to a target size would actually cost."""
    filled_usd: float
    expected_price_bp: float
    levels_consumed: int
    fully_filled: bool


@dataclass(frozen=True)
class Decision:
    mode: str
    permitted: bool
    abstain_reason: str | None
    side: str | None
    expected_acquisition_bp: float | None
    conservative_p_bp: int | None
    ev_per_share_bp: float | None
    intended_size_usd: float
    max_notional_usd: float
    cluster_exposure_cap_usd: float
    eligibility_status: str
    notes: list[str] = field(default_factory=list)

    @property
    def is_simulation(self) -> bool:
        """True when this decision is a counterfactual, not a permission."""
        return self.mode == PAPER


def walk_book(levels: list[BookLevel], target_usd: float) -> Fill:
    """
    Expected acquisition price for `target_usd` against a real book.

    Levels are consumed cheapest-first. A partial fill is reported as such rather
    than silently priced as if the whole size were available.
    """
    if target_usd <= 0:
        raise DecisionError("target size must be positive")
    if not levels:
        return Fill(0.0, 0.0, 0, False)

    remaining = target_usd
    cost = 0.0
    consumed = 0
    for lvl in sorted(levels, key=lambda x: x.price_bp):
        if remaining <= 0:
            break
        take = min(remaining, lvl.size_usd)
        cost += take * (lvl.price_bp / 10000.0)
        remaining -= take
        consumed += 1

    filled = target_usd - remaining
    if filled <= 0:
        return Fill(0.0, 0.0, 0, False)
    return Fill(
        filled_usd=filled,
        expected_price_bp=(cost / filled) * 10000.0,
        levels_consumed=consumed,
        fully_filled=remaining <= 1e-9,
    )


def conservative_probability_bp(p_est_bp: int, p_lo_bp: int, p_hi_bp: int,
                                side: str) -> int:
    """
    The end of the interval that argues *against* the trade.

    Buying YES uses the low end; buying NO uses the high end, since a NO position
    profits when the event does not occur. Using the point estimate for both
    would let a wide interval cost nothing.
    """
    if side == "YES":
        return p_lo_bp
    if side == "NO":
        return p_hi_bp
    raise DecisionError(f"side must be YES or NO, got {side!r}")


def expected_value_bp(conservative_p_bp: int, acquisition_bp: float,
                      costs_bp: float, side: str) -> float:
    """
    EV per share in basis points of $1, held to settlement.

        EV = settlement_value - acquisition - costs

    Costs are additive here and must not double-count the spread, which is
    already inside `acquisition_bp` via the book walk.
    """
    settlement = conservative_p_bp if side == "YES" else (10000 - conservative_p_bp)
    return settlement - acquisition_bp - costs_bp


def decide(
    *,
    p_est_bp: int,
    p_lo_bp: int,
    p_hi_bp: int,
    side: str,
    book: list[BookLevel],
    intended_size_usd: float,
    costs_bp: float,
    eligibility_status: str,
    max_notional_usd: float,
    cluster_exposure_used_usd: float,
    cluster_exposure_cap_usd: float,
    min_edge_bp: float = 100.0,
    require_full_fill: bool = True,
    mode: str = PAPER,
) -> Decision:
    """
    Form a trade decision. Abstains by default and explains every refusal.

    `min_edge_bp` is a floor on EV after costs — an edge smaller than the noise
    in our own price estimate is not an edge. Default 100bp (1c per share).

    `mode` defaults to PAPER, which evaluates the counterfactual and skips the
    jurisdictional eligibility gate while recording that it did. LIVE enforces
    eligibility — and there is deliberately no order-management service for it
    to feed, so a LIVE decision can be computed but not acted on.
    """
    if mode not in (PAPER, LIVE):
        raise DecisionError(f"mode must be {PAPER!r} or {LIVE!r}, got {mode!r}")
    notes: list[str] = []

    def abstain(reason: str, **kw) -> Decision:
        return Decision(
            mode=mode, permitted=False, abstain_reason=reason, side=side,
            intended_size_usd=intended_size_usd,
            max_notional_usd=max_notional_usd,
            cluster_exposure_cap_usd=cluster_exposure_cap_usd,
            eligibility_status=eligibility_status, notes=notes,
            expected_acquisition_bp=kw.get("acq"),
            conservative_p_bp=kw.get("cons"),
            ev_per_share_bp=kw.get("ev"),
        )

    # Eligibility first: no amount of edge makes an impermissible trade permissible.
    # In paper mode the counterfactual is the whole point, so the gate is noted
    # rather than enforced — but the note travels with the decision.
    if eligibility_status != TRADEABLE:
        if mode == LIVE:
            return abstain(f"not tradeable: eligibility is '{eligibility_status}'")
        notes.append(
            f"SIMULATED: eligibility is '{eligibility_status}', so this decision is a "
            "counterfactual and confers no permission to trade"
        )

    if intended_size_usd <= 0:
        return abstain("intended size is zero")
    if intended_size_usd > max_notional_usd:
        return abstain(
            f"intended size {intended_size_usd:.2f} exceeds max notional "
            f"{max_notional_usd:.2f}"
        )

    headroom = cluster_exposure_cap_usd - cluster_exposure_used_usd
    if intended_size_usd > headroom:
        return abstain(
            f"cluster exposure cap: {headroom:.2f} headroom, "
            f"{intended_size_usd:.2f} requested. Correlated positions are one "
            "bet economically, not several"
        )

    fill = walk_book(book, intended_size_usd)
    if fill.filled_usd <= 0:
        return abstain("no book depth at any price")
    if require_full_fill and not fill.fully_filled:
        return abstain(
            f"book depth insufficient: {fill.filled_usd:.2f} of "
            f"{intended_size_usd:.2f} available"
        )
    if not fill.fully_filled:
        notes.append(
            f"partial fill: {fill.filled_usd:.2f} of {intended_size_usd:.2f}"
        )

    cons = conservative_probability_bp(p_est_bp, p_lo_bp, p_hi_bp, side)
    ev = expected_value_bp(cons, fill.expected_price_bp, costs_bp, side)

    if ev < min_edge_bp:
        point_ev = expected_value_bp(
            p_est_bp if side == "YES" else p_est_bp,
            fill.expected_price_bp, costs_bp, side,
        )
        if point_ev >= min_edge_bp:
            notes.append(
                f"point estimate would give {point_ev:.0f}bp; uncertainty "
                "removes the edge"
            )
        return abstain(
            f"EV {ev:.0f}bp below the {min_edge_bp:.0f}bp floor after costs",
            acq=fill.expected_price_bp, cons=cons, ev=ev,
        )

    return Decision(
        mode=mode, permitted=True, abstain_reason=None, side=side,
        expected_acquisition_bp=fill.expected_price_bp,
        conservative_p_bp=cons, ev_per_share_bp=ev,
        intended_size_usd=fill.filled_usd,
        max_notional_usd=max_notional_usd,
        cluster_exposure_cap_usd=cluster_exposure_cap_usd,
        eligibility_status=eligibility_status, notes=notes,
    )


def record(
    con,
    *,
    forecast_id: int,
    contract_id: int,
    decided_at: str,
    d: Decision,
    costs_bp: float,
    book_snapshot_ref: str | None = None,
) -> int:
    """
    Persist a decision. The only writer to `trade_decisions`.

    It exists so that `mode` cannot be omitted. The schema column carries no
    default precisely because the omission would be silent and would relabel a
    live decision as a simulation; routing every write through one function is
    what makes that guarantee hold in practice rather than in principle.

    Notes are stored on the abstain reason for a refusal and discarded for a
    permission, because the note that matters on a permitted paper decision --
    that it is a counterfactual -- is already implied by `mode`.
    """
    reason = d.abstain_reason
    if reason is None and not d.permitted:
        raise DecisionError("a refusal must carry a reason")
    cur = con.execute(
        """INSERT INTO trade_decisions
           (forecast_id, contract_id, decided_at, expected_acquisition_bp,
            intended_size_usd, costs_bp, ev_per_share_bp, permitted,
            abstain_reason, mode, max_notional_usd, cluster_exposure_cap_usd,
            eligibility_status, book_snapshot_ref)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (forecast_id, contract_id, decided_at,
         round(d.expected_acquisition_bp) if d.expected_acquisition_bp is not None else None,
         d.intended_size_usd, round(costs_bp), d.ev_per_share_bp,
         1 if d.permitted else 0, reason, d.mode,
         d.max_notional_usd, d.cluster_exposure_cap_usd,
         d.eligibility_status, book_snapshot_ref),
    )
    con.commit()
    return cur.lastrowid

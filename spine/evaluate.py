"""
Closing the loop: resolutions to scores, and scores to a verdict.

Three tables the project defined and never populated — `scores`, `settlements`,
`forecast_edges` — plus the step nobody had written: building `Observation`s
*from the database* rather than from a list someone assembled by hand. Until
now `spine/scoring.py` could score anything except the record.

Four disciplines, each the difference between a real number and a flattering one.

**A forecast is scored against its proposition, not its contract.** Research
outcome and economic settlement are separate objects (§10.1). A contract that
settled at 0.50 under a UMA "Unknown" has an economic result and no research
result; a proposition can resolve cleanly while its contract is still disputed.
Conflating them lets a position vanish from P&L or an outcome vanish from Brier,
depending which way you lean.

**Exclusions are counted, never dropped.** Every unscorable forecast gets a row
with a reason. Silently skipping them shrinks the denominator, and a shrinking
denominator makes skill look better — so the exclusion rate is reported next to
the score, and a high one is itself the finding.

**Nothing is scored before its label was knowable.** `label_available_at` gates
scoring the same way `available_for_decision_at` gates retrieval.

**Comparisons are within (kind, model version).** A Brier average over a mixed
bag of independent and market-conditioned forecasts from three model versions
measures the mixture, not the method.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass, field

from . import timeutil
from .scoring import (
    MIN_REGIMES,
    Observation,
    ScoringError,
    brier,
    bss,
    calibration_curve,
    estimate_r_between,
    murphy,
    n_eff_ceiling,
    regime_bootstrap_ci,
)

# One timestamp format for the whole project. spine/timeutil.py has the
# lexicographic-ordering bug that made this non-negotiable.
_now = timeutil.now

SCORABLE_OUTCOMES = {"resolved_yes", "resolved_no"}

# Why a forecast could not be scored. Each is a row in `scores`, not a silence.
EXCLUSIONS = {
    "void": "the proposition was voided; there is no outcome to score against",
    "disputed": "the outcome is disputed and not yet adjudicated",
    "unresolvable": "the proposition cannot be resolved as written",
    "unresolved": "no resolution recorded yet",
    "label_not_available": "the outcome was not yet knowable at the scoring time",
}


class EvaluationError(RuntimeError):
    """An evaluation step would have produced a number that means something else."""


# ---------------------------------------------------------------------------
# scoring the record
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringRun:
    scored: int
    excluded: dict[str, int] = field(default_factory=dict)
    already_scored: int = 0

    @property
    def total(self) -> int:
        return self.scored + sum(self.excluded.values()) + self.already_scored

    @property
    def exclusion_rate(self) -> float:
        considered = self.scored + sum(self.excluded.values())
        return sum(self.excluded.values()) / considered if considered else 0.0

    def summary(self) -> str:
        s = (f"{self.scored} scored, {sum(self.excluded.values())} excluded "
             f"({self.exclusion_rate:.0%}), {self.already_scored} already had scores")
        if self.excluded:
            s += " — " + ", ".join(f"{n} {r}" for r, n in
                                   sorted(self.excluded.items(), key=lambda kv: -kv[1]))
        return s


def score_all(con: sqlite3.Connection, as_of: str | None = None) -> ScoringRun:
    """
    Write a `scores` row for every forecast, scorable or not.

    Already-scored forecasts are left alone rather than recomputed. A score that
    silently changes is worse than no score: the whole point of the hash chain
    upstream is that the record does not move, and a scoring pass that rewrote
    history would undo it one table lower down.
    """
    ts = timeutil.canonical(as_of) if as_of else _now()
    rows = con.execute(
        """SELECT f.id, f.p_est_bp, f.label_available_at, r.outcome
           FROM forecasts f
           LEFT JOIN resolutions r ON r.proposition_id = f.proposition_id
           WHERE f.created_at <= ?
             AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.forecast_id = f.id)
           ORDER BY f.id""", (ts,)).fetchall()
    already = con.execute("SELECT COUNT(*) FROM scores").fetchone()[0]

    scored = 0
    excluded: dict[str, int] = {}

    for fid, p_est_bp, label_at, outcome in rows:
        reason = None
        if outcome is None:
            reason = "unresolved"
        elif outcome not in SCORABLE_OUTCOMES:
            reason = outcome
        elif label_at is not None and label_at > ts:
            reason = "label_not_available"

        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            con.execute(
                "INSERT INTO scores(forecast_id, brier, scorable, exclusion_reason, "
                "computed_at) VALUES(?,NULL,0,?,?)",
                (fid, EXCLUSIONS.get(reason, reason), ts))
            continue

        y = 1.0 if outcome == "resolved_yes" else 0.0
        b = (p_est_bp / 10000.0 - y) ** 2
        con.execute(
            "INSERT INTO scores(forecast_id, brier, scorable, exclusion_reason, "
            "computed_at) VALUES(?,?,1,NULL,?)", (fid, b, ts))
        scored += 1

    con.commit()
    return ScoringRun(scored, excluded, already)


def observations(
    con: sqlite3.Connection,
    as_of: str | None = None,
    *,
    forecast_kind: str | None = None,
    model_version: str | None = None,
    event_family: str | None = None,
) -> list[Observation]:
    """
    Scorable observations from the record, keyed by proposition for pairing.

    Filters are offered because comparing across them is the error: a Brier
    average over two model versions measures the mixture. Passing none is
    allowed and is the right thing for an overall calibration plot, which is a
    different question from a skill comparison.
    """
    sql = """SELECT f.p_est_bp, f.p_base_bp, f.regime_id, f.proposition_id,
                    r.outcome
             FROM forecasts f
             JOIN scores s ON s.forecast_id = f.id AND s.scorable = 1
             JOIN resolutions r ON r.proposition_id = f.proposition_id
             JOIN propositions pr ON pr.id = f.proposition_id
             WHERE 1=1"""
    args: list = []
    if as_of:
        sql += " AND s.computed_at <= ?"
        args.append(as_of)
    if forecast_kind:
        sql += " AND f.forecast_kind = ?"
        args.append(forecast_kind)
    if model_version:
        sql += " AND f.model_version = ?"
        args.append(model_version)
    if event_family:
        sql += " AND pr.event_family = ?"
        args.append(event_family)
    sql += " ORDER BY f.id"

    out = []
    for p_est, p_base, regime, prop_id, outcome in con.execute(sql, args):
        out.append(Observation(
            p=p_est / 10000.0,
            outcome=1 if outcome == "resolved_yes" else 0,
            p_base=p_base / 10000.0,
            regime_id=regime,
            key=f"prop:{prop_id}"))
    return out


# ---------------------------------------------------------------------------
# settlement: the economic outcome, kept separate
# ---------------------------------------------------------------------------

def record_settlement(
    con: sqlite3.Connection,
    contract_id: int,
    settlement_state: str,
    payout_per_share: float,
    settled_at: str | None = None,
    note: str | None = None,
    recorded_at: str | None = None,
) -> int:
    """
    Record what a contract actually paid.

    Deliberately accepts a payout that disagrees with the research outcome. A
    UMA "Unknown" settles at 0.50 on a proposition that resolved cleanly YES,
    and forcing consistency here would erase exactly the discrepancy worth
    knowing about — `settlement_divergence()` is for finding those, not for
    preventing them.
    """
    cur = con.execute(
        """INSERT OR REPLACE INTO settlements
           (contract_id, settlement_state, payout_per_share, settled_at,
            recorded_at, note)
           VALUES (?,?,?,?,?,?)""",
        (contract_id, settlement_state, payout_per_share, settled_at,
         timeutil.canonical(recorded_at) if recorded_at else _now(), note))
    con.commit()
    return cur.lastrowid


def settlement_divergence(con: sqlite3.Connection) -> list[dict]:
    """
    Contracts whose payout disagrees with the research outcome of the
    proposition bound to them.

    Every row here is a case where the instrument did not measure the question —
    the concrete version of §10.1's "material mismatch", found after the fact
    instead of argued about beforehand.
    """
    cur = con.execute(
        """SELECT c.id AS contract_id, c.market_id, s.settlement_state,
                  s.payout_per_share, r.outcome, b.match_quality
           FROM settlements s
           JOIN contracts c ON c.id = s.contract_id
           JOIN proposition_contract_binding b ON b.contract_id = c.id
           JOIN resolutions r ON r.proposition_id = b.proposition_id
           WHERE (r.outcome = 'resolved_yes' AND s.payout_per_share < 1.0)
              OR (r.outcome = 'resolved_no'  AND s.payout_per_share > 0.0)
           ORDER BY c.id""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# the forecast DAG
# ---------------------------------------------------------------------------

def link(
    con: sqlite3.Connection,
    parent_hash: str,
    child_hash: str,
    *,
    conditional_prob: float,
    dependence_kind: str,
    correlation: float,
    created_at: str | None = None,
) -> None:
    """
    Record that two forecasts are not independent.

    `dependence_kind` is not decoration. **Score correlation is not loss
    correlation**: two forecasts can be scored almost identically and lose money
    at different times, or be scored independently and blow up together. The
    schema keeps a separate DAG per kind and refuses a cycle within each, so
    "these are correlated" is always a statement about *which* correlation.
    """
    if dependence_kind not in ("score", "loss", "evidence"):
        raise EvaluationError(
            f"dependence_kind must be score/loss/evidence, got {dependence_kind!r}")
    con.execute(
        """INSERT INTO forecast_edges
           (parent_hash, child_hash, conditional_prob, dependence_kind,
            correlation, created_at)
           VALUES (?,?,?,?,?,?)""",
        (parent_hash, child_hash, conditional_prob, dependence_kind, correlation,
         timeutil.canonical(created_at) if created_at else _now()))
    con.commit()


def dependents(con: sqlite3.Connection, forecast_hash: str,
               dependence_kind: str) -> list[str]:
    """Everything reachable from a forecast along one kind of dependence."""
    cur = con.execute(
        """WITH RECURSIVE reach(n) AS (
               SELECT child_hash FROM forecast_edges
               WHERE parent_hash = ? AND dependence_kind = ?
               UNION
               SELECT e.child_hash FROM forecast_edges e
               JOIN reach r ON e.parent_hash = r.n
               WHERE e.dependence_kind = ?
           ) SELECT n FROM reach ORDER BY n""",
        (forecast_hash, dependence_kind, dependence_kind))
    return [r[0] for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# the readout
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Evaluation:
    n: int
    n_regimes: int
    brier: float | None
    bss: float | None
    reliability: float | None
    resolution: float | None
    r_between: float | None
    n_eff_ceiling: float | None
    ci_point: float | None = None
    ci_lower: float | None = None
    ci_upper: float | None = None
    gate_fires: bool = False
    blocked_by: str | None = None

    def summary(self) -> str:
        if not self.n:
            return "no scorable observations yet"
        lines = [f"n={self.n} across {self.n_regimes} regime(s)",
                 f"Brier {self.brier:.4f}  BSS {self.bss:+.4f}",
                 f"reliability {self.reliability:.5f}  "
                 f"resolution {self.resolution:.5f}"]
        if self.r_between is not None:
            lines.append(f"r_between {self.r_between:.4f} "
                         f"(n_eff ceiling {self.n_eff_ceiling:.0f})")
        if self.blocked_by:
            lines.append(f"gate: NOT EVALUABLE — {self.blocked_by}")
        else:
            lines.append(f"gate: {'FIRES' if self.gate_fires else 'does not fire'} "
                         f"[{self.ci_lower:+.4f}, {self.ci_upper:+.4f}]")
        return "\n".join(lines)


def evaluate(
    con: sqlite3.Connection,
    as_of: str | None = None,
    *,
    resamples: int = 2000,
    **filters,
) -> Evaluation:
    """
    The whole readout for one slice of the record.

    Below twelve regimes this reports the descriptive statistics and says the
    gate is **not evaluable** — it does not report a narrower interval with a
    caveat attached. Phase 1 measured a 43.5% false positive rate at one regime;
    a number produced there is worse than no number, because a number gets
    quoted and a refusal does not.
    """
    obs = observations(con, as_of, **filters)
    if not obs:
        return Evaluation(0, 0, None, None, None, None, None, None,
                          blocked_by="no scorable observations")

    m = murphy(obs)
    n_regimes = len({o.regime_id for o in obs})
    r_b = estimate_r_between(obs) if n_regimes >= 2 else None

    base = dict(n=len(obs), n_regimes=n_regimes, brier=brier(obs), bss=bss(obs),
                reliability=m["reliability"], resolution=m["resolution"],
                r_between=r_b,
                n_eff_ceiling=n_eff_ceiling(r_b) if r_b is not None else None)
    try:
        ci = regime_bootstrap_ci(obs, resamples=resamples)
    except ScoringError as e:
        return Evaluation(**base, blocked_by=str(e).split(".")[0])
    return Evaluation(**base, ci_point=ci.point, ci_lower=ci.lower,
                      ci_upper=ci.upper, gate_fires=ci.fires)


def calibration(con: sqlite3.Connection, bins: int = 10, **filters) -> list[dict]:
    """Calibration table from the record. Reported *alongside* skill, never instead."""
    obs = observations(con, **filters)
    return calibration_curve(obs, bins=bins) if obs else []


def slices(con: sqlite3.Connection) -> list[dict]:
    """
    Every (kind, model version, family) combination present, with its count.

    Exists so that "evaluate the record" cannot quietly mean "average over
    things that should not be averaged". Look here before choosing filters.
    """
    cur = con.execute(
        """SELECT f.forecast_kind, f.model_version, pr.event_family,
                  COUNT(*) AS n, COUNT(DISTINCT f.regime_id) AS regimes
           FROM forecasts f JOIN propositions pr ON pr.id = f.proposition_id
           GROUP BY 1,2,3 ORDER BY n DESC""")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

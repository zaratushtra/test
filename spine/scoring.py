"""
Scoring: Brier, Murphy decomposition, and the regime-level bootstrap.

Phase 1 established that the originally specified gate — a cluster bootstrap over
weeks — is invalid, not merely slow. A component shared across all observations
(one analyst, one frozen model version, one macro regime) makes the grand mean
inconsistent: its variance tends to gamma^2 rather than zero, while a week-level
bootstrap cannot see it, because every resample contains it. Measured false
positive rate reached 38%, and running five times longer made it marginally worse.

So this module deliberately offers **no** week-level or observation-level
bootstrap. The only path is `regime_bootstrap_ci`, and it refuses to return a
result below the minimum regime count rather than returning a confident-looking
number nobody should trust. An invalid analysis that is merely documented as
invalid eventually gets run.

Two further disciplines the design demands and this module enforces:

  * **Skill is not calibration.** `bss()` and `reliability()` are separate calls
    returning separate numbers, because a model can improve Brier through
    resolution while staying systematically overconfident in a region.
  * **Unscorable outcomes are excluded, not counted as misses.** A void or
    disputed resolution carries no Brier score; dropping it silently into the
    "wrong" bucket would penalise a forecast for an oracle's behaviour.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass

# Phase 1 measurement: 12 regimes restored a nominal 4.0% false positive rate,
# 4 regimes gave 13.0%, and 1 regime gave 43.5%. See docs/PHASE1-REPORT.md.
MIN_REGIMES = 12

SCORABLE = {"resolved_yes", "resolved_no"}


class ScoringError(RuntimeError):
    """The requested analysis would not be valid."""


@dataclass(frozen=True)
class Observation:
    """One scored forecast."""
    p: float            # forecast probability
    outcome: int        # 1 or 0
    p_base: float       # benchmark the skill score is measured against
    regime_id: str

    def brier(self) -> float:
        return (self.p - self.outcome) ** 2

    def base_brier(self) -> float:
        return (self.p_base - self.outcome) ** 2

    def diff(self) -> float:
        """Paired score difference: positive means the model beat the benchmark."""
        return self.base_brier() - self.brier()


@dataclass(frozen=True)
class BootstrapResult:
    point: float
    lower: float
    upper: float
    n_regimes: int
    n_observations: int
    resamples: int

    @property
    def fires(self) -> bool:
        """Whether the gate passes: the lower bound clears zero."""
        return self.lower > 0.0


def to_observations(rows: list[dict]) -> list[Observation]:
    """
    Build scorable observations, dropping unscorable outcomes.

    `rows` need: p_est_bp, p_base_bp, regime_id, outcome.
    """
    out = []
    for r in rows:
        if r["outcome"] not in SCORABLE:
            continue
        out.append(
            Observation(
                p=r["p_est_bp"] / 10000.0,
                outcome=1 if r["outcome"] == "resolved_yes" else 0,
                p_base=r["p_base_bp"] / 10000.0,
                regime_id=r["regime_id"],
            )
        )
    return out


def brier(obs: list[Observation]) -> float:
    if not obs:
        raise ScoringError("no scorable observations")
    return statistics.fmean(o.brier() for o in obs)


def bss(obs: list[Observation]) -> float:
    """Brier skill score against the recorded benchmark. Skill, not calibration."""
    if not obs:
        raise ScoringError("no scorable observations")
    b_model = statistics.fmean(o.brier() for o in obs)
    b_base = statistics.fmean(o.base_brier() for o in obs)
    if b_base == 0:
        raise ScoringError("benchmark Brier is zero; skill is undefined")
    return 1.0 - b_model / b_base


def murphy(obs: list[Observation], bins: int = 10) -> dict:
    """
    Decompose Brier into reliability, resolution and uncertainty.

        B = reliability - resolution + uncertainty + within_bin_residual

    Reliability is the calibration term (lower is better). Resolution is
    discrimination (higher is better). Reporting only BSS hides which one moved.

    The classical identity drops the last term because it is exact only for
    *discrete* forecast values, where every forecast in a bin is identical.
    With continuous forecasts the residual is the within-bin variance; measured
    at 2.8e-17 for discrete inputs and shrinking monotonically toward zero as
    bins approach the number of distinct values. It is returned rather than
    hidden, because a decomposition that silently fails to add up is worse than
    one that reports its own slack.

    Note that binned `reliability` is biased upward as bins get finer — fewer
    observations per bin make the observed rate noisier. Compare reliability
    across models at a fixed bin count, never across bin counts.
    """
    if not obs:
        raise ScoringError("no scorable observations")
    n = len(obs)
    obar = statistics.fmean(o.outcome for o in obs)

    buckets: dict[int, list[Observation]] = {}
    for o in obs:
        idx = min(bins - 1, int(o.p * bins))
        buckets.setdefault(idx, []).append(o)

    reliability = resolution = 0.0
    for members in buckets.values():
        nk = len(members)
        pk = statistics.fmean(m.p for m in members)
        ok = statistics.fmean(m.outcome for m in members)
        reliability += nk * (pk - ok) ** 2
        resolution += nk * (ok - obar) ** 2
    reliability /= n
    resolution /= n
    uncertainty = obar * (1 - obar)

    return {
        "brier": brier(obs),
        "reliability": reliability,
        "resolution": resolution,
        "uncertainty": uncertainty,
        "within_bin_residual": brier(obs) - (reliability - resolution + uncertainty),
        "base_rate": obar,
        "n": n,
    }


def calibration_curve(obs: list[Observation], bins: int = 10) -> list[dict]:
    """Per-bin forecast vs observed frequency. Reported alongside BSS, never instead."""
    buckets: dict[int, list[Observation]] = {}
    for o in obs:
        buckets.setdefault(min(bins - 1, int(o.p * bins)), []).append(o)
    rows = []
    for idx in sorted(buckets):
        members = buckets[idx]
        rows.append({
            "bin_lo": idx / bins,
            "bin_hi": (idx + 1) / bins,
            "n": len(members),
            "mean_forecast": statistics.fmean(m.p for m in members),
            "observed_rate": statistics.fmean(m.outcome for m in members),
        })
    return rows


def regime_bootstrap_ci(
    obs: list[Observation],
    resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
    min_regimes: int = MIN_REGIMES,
) -> BootstrapResult:
    """
    Bootstrap the paired score difference by resampling whole regimes.

    Refuses below `min_regimes`. This is not a warning: Phase 1 measured a 43.5%
    false positive rate at one regime, so a number returned there would be worse
    than no number at all.
    """
    if not obs:
        raise ScoringError("no scorable observations")

    by_regime: dict[str, list[Observation]] = {}
    for o in obs:
        by_regime.setdefault(o.regime_id, []).append(o)

    regimes = sorted(by_regime)
    if len(regimes) < min_regimes:
        raise ScoringError(
            f"{len(regimes)} regime(s) present, {min_regimes} required. "
            "Phase 1 measured false positive rates of 43.5% at 1 regime and "
            "13.0% at 4; the interval would be far too narrow to mean anything. "
            "Rotate the shared component (model version, analyst, macro period) "
            "and re-run."
        )

    regime_means = [statistics.fmean(o.diff() for o in by_regime[r]) for r in regimes]
    point = statistics.fmean(regime_means)

    rng = random.Random(seed)
    k = len(regime_means)
    stats = sorted(
        statistics.fmean([regime_means[rng.randrange(k)] for _ in range(k)])
        for _ in range(resamples)
    )
    lo_i = max(0, min(len(stats) - 1, int((alpha / 2) * len(stats))))
    hi_i = max(0, min(len(stats) - 1, int((1 - alpha / 2) * len(stats))))

    return BootstrapResult(
        point=point,
        lower=stats[lo_i],
        upper=stats[hi_i],
        n_regimes=k,
        n_observations=len(obs),
        resamples=resamples,
    )


def estimate_r_between(obs: list[Observation]) -> float:
    """
    Estimate the between-regime correlation of paired score differences.

    One-way random-effects ICC on regime means. This is the quantity Phase 1
    showed imposes a hard ceiling of 1/r on effective sample size, and the design
    now requires it be *measured* from the record rather than assumed.

    Returns 0.0 when the between-regime component is not distinguishable from
    noise — an estimate, not a guarantee of independence.
    """
    by_regime: dict[str, list[Observation]] = {}
    for o in obs:
        by_regime.setdefault(o.regime_id, []).append(o)
    if len(by_regime) < 2:
        raise ScoringError("need at least 2 regimes to estimate between-regime correlation")

    groups = [[o.diff() for o in v] for v in by_regime.values()]
    n_total = sum(len(g) for g in groups)
    grand = statistics.fmean(d for g in groups for d in g)
    k = len(groups)

    ss_between = sum(len(g) * (statistics.fmean(g) - grand) ** 2 for g in groups)
    ss_within = sum((d - statistics.fmean(g)) ** 2 for g in groups for d in g)
    if n_total - k <= 0 or k - 1 <= 0:
        return 0.0

    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n_total - k)
    n0 = (n_total - sum(len(g) ** 2 for g in groups) / n_total) / (k - 1)
    if n0 <= 0 or ms_within <= 0:
        return 0.0

    var_between = (ms_between - ms_within) / n0
    if var_between <= 0:
        return 0.0
    return var_between / (var_between + ms_within)


def n_eff_ceiling(r_between: float) -> float:
    """Effective sample size cannot exceed this, however long the record runs."""
    return float("inf") if r_between <= 0 else 1.0 / r_between

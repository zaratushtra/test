#!/usr/bin/env python3
"""
Phase 1 — serial dependence and the real power requirement.

SPINE-DESIGN-V2 §3.2 flagged this as the largest open risk. v1 assumed weekly
effective sample sizes add up:

    8/week at within-week r=0.05  ->  5.93/week  ->  x42 weeks  ->  ~249

That holds only if weeks are independent of each other. They are not obviously
so: the same analyst, the same frozen model version and the same macro regime
produce a component shared across *every* observation, not just within a week.

This module answers the question analytically and then checks the analysis
against an empirical simulation of the actual decision rule.

The result is not a tuning detail. Under equicorrelation, effective sample size
has a hard ceiling of 1/r_between no matter how long the experiment runs — so a
between-week correlation of 0.05 caps n_eff at 20 forever, against a requirement
of ~314. Running longer cannot fix it; only reducing the shared component can.

Model (three-level, deliberately the simplest thing that has the property):

    d_ij = mu + g + u_i + e_ij

    g    ~ N(0, gamma^2)   shared by ALL observations  (analyst, model, regime)
    u_i  ~ N(0, tau^2)     shared within week i        (news cycle, theme)
    e_ij ~ N(0, sigma^2)   idiosyncratic

    r_within  = (gamma^2 + tau^2) / V
    r_between =  gamma^2          / V        where V = gamma^2 + tau^2 + sigma^2

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys

Z_ALPHA = 1.96      # one-sided 2.5%, matching a 95% CI lower bound
Z_BETA = 0.8416     # 80% power
POWER_COEFFICIENT = (Z_ALPHA + Z_BETA) ** 2 * 4   # 31.4, see v2 section 3.1


# ---------------------------------------------------------------------------
# analytic
# ---------------------------------------------------------------------------

def n_eff(n: int, r: float) -> float:
    """Kish effective sample size for equicorrelated observations."""
    if n <= 0:
        return 0.0
    denom = 1.0 + (n - 1) * r
    return n / denom if denom > 0 else float("inf")


def n_eff_ceiling(r_between: float) -> float:
    """
    The limit of n_eff as the number of observations grows without bound.

    n/(1+(n-1)r) -> 1/r. This is the number that makes serial dependence fatal
    rather than merely inconvenient: it does not improve with time.
    """
    return float("inf") if r_between <= 0 else 1.0 / r_between


def n_eff_two_level(n_weeks: int, k_per_week: int, r_within: float,
                    r_between: float) -> float:
    """
    Effective sample size for a block-correlated design.

    Derived from the variance of the grand mean. With N = n_weeks * k,
    Var(mean) = V/N * [1 + (k-1)*r_w + k*(n_weeks-1)*r_b], and n_eff is the N
    that an independent sample would need to match it.
    """
    if n_weeks <= 0 or k_per_week <= 0:
        return 0.0
    N = n_weeks * k_per_week
    inflation = 1.0 + (k_per_week - 1) * r_within + k_per_week * (n_weeks - 1) * r_between
    return N / inflation if inflation > 0 else float("inf")


def required_n_eff(bss: float) -> float:
    return POWER_COEFFICIENT / bss if bss > 0 else float("inf")


def max_r_between(target_n_eff: float) -> float:
    """The largest between-week correlation under which the target is reachable."""
    return 1.0 / target_n_eff if target_n_eff > 0 else 0.0


def weeks_needed(k_per_week: int, r_within: float, r_between: float,
                 target: float, cap_weeks: int = 5200) -> int | None:
    """Weeks to reach the target n_eff, or None if the ceiling forbids it."""
    if n_eff_ceiling(r_between) <= target:
        return None
    lo, hi = 1, cap_weeks
    if n_eff_two_level(hi, k_per_week, r_within, r_between) < target:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if n_eff_two_level(mid, k_per_week, r_within, r_between) >= target:
            hi = mid
        else:
            lo = mid + 1
    return lo


# ---------------------------------------------------------------------------
# empirical — simulate the actual decision rule
# ---------------------------------------------------------------------------

def simulate_trial(rng: random.Random, n_weeks: int, k: int, mu: float,
                   gamma: float, tau: float, sigma: float) -> list[list[float]]:
    """One experiment: per-week lists of paired score differences."""
    g = rng.gauss(0.0, gamma) if gamma > 0 else 0.0
    weeks = []
    for _ in range(n_weeks):
        u = rng.gauss(0.0, tau) if tau > 0 else 0.0
        weeks.append([mu + g + u + rng.gauss(0.0, sigma) for _ in range(k)])
    return weeks


def cluster_bootstrap_lower(rng: random.Random, weeks: list[list[float]],
                            resamples: int) -> float:
    """
    2.5th percentile of the grand mean under a *cluster* bootstrap.

    Resamples whole weeks with replacement. Resampling individual observations
    would assume the independence this module exists to question — which is the
    error v2 section 5.6 warns about, appearing inside the gate itself.
    """
    week_means = [statistics.fmean(w) for w in weeks]
    n = len(week_means)
    stats = []
    for _ in range(resamples):
        stats.append(statistics.fmean(
            [week_means[rng.randrange(n)] for _ in range(n)]
        ))
    stats.sort()
    idx = max(0, min(len(stats) - 1, int(0.025 * len(stats))))
    return stats[idx]


def simulate_regimes(rng: random.Random, n_regimes: int, weeks_per_regime: int,
                     k: int, mu: float, gamma: float, tau: float,
                     sigma: float) -> list[list[float]]:
    """
    Same generative model, but the shared component is redrawn per regime.

    A "regime" is whatever the shared component is constant within: one frozen
    model version, one analyst, one macro period. Rotating them is the only
    thing that turns g from an unmeasurable bias into a sampled quantity.
    Returns per-regime pooled observations.
    """
    out = []
    for _ in range(n_regimes):
        g = rng.gauss(0.0, gamma) if gamma > 0 else 0.0
        vals = []
        for _ in range(weeks_per_regime):
            u = rng.gauss(0.0, tau) if tau > 0 else 0.0
            vals.extend(mu + g + u + rng.gauss(0.0, sigma) for _ in range(k))
        out.append(vals)
    return out


def regime_bootstrap_lower(rng: random.Random, regimes: list[list[float]],
                           resamples: int) -> float:
    """Bootstrap over regimes — the level at which the shared component varies."""
    means = [statistics.fmean(r) for r in regimes]
    n = len(means)
    stats = sorted(
        statistics.fmean([means[rng.randrange(n)] for _ in range(n)])
        for _ in range(resamples)
    )
    idx = max(0, min(len(stats) - 1, int(0.025 * len(stats))))
    return stats[idx]


def _components(r_within: float, r_between: float) -> tuple[float, float, float]:
    """Variance components for the requested correlations, normalised to V = 1."""
    return (
        math.sqrt(r_between),
        math.sqrt(max(0.0, r_within - r_between)),
        math.sqrt(max(0.0, 1.0 - r_within)),
    )


def empirical_power_regimes(n_regimes: int, weeks_per_regime: int, k: int,
                            true_effect: float, r_within: float, r_between: float,
                            trials: int, resamples: int, seed: int = 7) -> float:
    """False-positive rate / power when the gate bootstraps over regimes."""
    gamma, tau, sigma = _components(r_within, r_between)
    rng = random.Random(seed)
    fired = 0
    for _ in range(trials):
        regimes = simulate_regimes(rng, n_regimes, weeks_per_regime, k,
                                   true_effect, gamma, tau, sigma)
        if regime_bootstrap_lower(rng, regimes, resamples) > 0:
            fired += 1
    return fired / trials


def empirical_power(n_weeks: int, k: int, true_effect: float, r_within: float,
                    r_between: float, trials: int, resamples: int,
                    seed: int = 7) -> float:
    """Fraction of trials where the gate fires. true_effect=0 gives the false-positive rate."""
    gamma, tau, sigma = _components(r_within, r_between)
    rng = random.Random(seed)
    fired = 0
    for _ in range(trials):
        weeks = simulate_trial(rng, n_weeks, k, true_effect, gamma, tau, sigma)
        if cluster_bootstrap_lower(rng, weeks, resamples) > 0:
            fired += 1
    return fired / trials


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE Phase 1 serial dependence analysis")
    ap.add_argument("--k-per-week", type=int, default=8)
    ap.add_argument("--r-within", type=float, default=0.05)
    ap.add_argument("--target-bss", type=float, default=0.10)
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--resamples", type=int, default=400)
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    target = required_n_eff(args.target_bss)
    k = args.k_per_week
    rw = args.r_within

    print("=" * 76)
    print("SPINE PHASE 1 — SERIAL DEPENDENCE AND THE REAL POWER REQUIREMENT")
    print("=" * 76)
    print(f"target BSS {args.target_bss:.2f}  ->  n_eff required {target:.0f}"
          f"   (coefficient {POWER_COEFFICIENT:.1f})")
    print(f"basket {k}/week, within-week r = {rw:.3f}\n")

    print(f"{'r_between':>10}{'n_eff ceiling':>15}{'n_eff @1yr':>12}"
          f"{'n_eff @5yr':>12}{'weeks to target':>18}")
    print("-" * 76)
    rows = []
    for rb in (0.0, 0.001, 0.0032, 0.005, 0.01, 0.02, 0.05, 0.10, rw):
        if rb > rw:
            continue
        ceil = n_eff_ceiling(rb)
        y1 = n_eff_two_level(52, k, rw, rb)
        y5 = n_eff_two_level(260, k, rw, rb)
        w = weeks_needed(k, rw, rb, target)
        ceil_s = "unbounded" if math.isinf(ceil) else f"{ceil:.0f}"
        w_s = "UNREACHABLE" if w is None else (f"{w}" if w <= 520 else f"{w} (~{w/52:.0f}y)")
        print(f"{rb:>10.4f}{ceil_s:>15}{y1:>12.1f}{y5:>12.1f}{w_s:>18}")
        rows.append({"r_between": rb, "ceiling": None if math.isinf(ceil) else ceil,
                     "n_eff_1yr": y1, "n_eff_5yr": y5, "weeks_to_target": w})

    crit = max_r_between(target)
    print("-" * 76)
    print(f"\nCRITICAL THRESHOLD: between-week correlation must stay below "
          f"{crit:.5f}\n  for n_eff = {target:.0f} to be reachable AT ALL. Above it, no "
          "amount of time suffices:\n  the ceiling is 1/r_between and it does not move.")

    print(f"\n{'-'*76}\nEMPIRICAL CHECK — simulating the actual gate "
          f"(cluster bootstrap, 2.5th pct)\n{'-'*76}")
    print(f"{'r_between':>10}{'weeks':>8}{'true effect':>13}{'gate fires':>12}"
          f"   interpretation")
    emp = []
    # Effect sized so that an independent design of this length would be adequately
    # powered; the question is what serial dependence does to that.
    for rb, weeks in ((0.0, 52), (0.005, 52), (0.05, 52), (0.05, 260)):
        for effect in (0.0, 0.25):
            p = empirical_power(weeks, k, effect, rw, rb, args.trials, args.resamples)
            kind = "false positive rate" if effect == 0.0 else "power"
            flag = ""
            if effect == 0.0 and p > 0.08:
                flag = "  <- INFLATED"
            if effect > 0.0 and p < 0.5:
                flag = "  <- UNDERPOWERED"
            print(f"{rb:>10.4f}{weeks:>8}{effect:>13.2f}{p:>12.1%}   {kind}{flag}")
            emp.append({"r_between": rb, "weeks": weeks, "true_effect": effect,
                        "rate": p, "kind": kind})

    print(f"\n{'-'*76}\nTHE FIX — bootstrap over REGIMES, the level at which g varies\n{'-'*76}")
    print(f"{'regimes':>9}{'wks each':>10}{'true effect':>13}{'gate fires':>12}"
          f"   interpretation")
    fix = []
    for nreg, wpr in ((1, 52), (4, 13), (12, 13), (24, 13)):
        for effect in (0.0, 0.25):
            p_ = empirical_power_regimes(nreg, wpr, k, effect, rw, 0.05,
                                         args.trials, args.resamples)
            kind = "false positive rate" if effect == 0.0 else "power"
            flag = ""
            if effect == 0.0:
                flag = "  <- INFLATED" if p_ > 0.08 else "  <- nominal"
            print(f"{nreg:>9}{wpr:>10}{effect:>13.2f}{p_:>12.1%}   {kind}{flag}")
            fix.append({"regimes": nreg, "weeks_per_regime": wpr,
                        "true_effect": effect, "rate": p_, "kind": kind})

    print(f"\n{'='*76}")
    print("FINDING")
    print("=" * 76)
    print(f"""
v1 assumed weekly effective sample sizes accumulate. They do not. A component
shared across weeks imposes a ceiling of 1/r_between on n_eff that no amount of
running time can lift.

For the current gate (BSS {args.target_bss:.2f}, n_eff {target:.0f}), between-week correlation must
stay below {crit:.5f}. That is a demanding requirement: a single analyst applying
one frozen model version across a stable macro regime plausibly exceeds it.

This does not sink the project, but it relocates the hard problem. The lever is
no longer "run longer" — it is reducing the shared component: rotating model
versions, diversifying event families across regimes, and measuring r_between
directly from resolved forecasts rather than assuming it away.

MEASUREMENT IS NOW A DELIVERABLE, not an assumption. Phase 3 must estimate
r_between from the accumulating record and re-derive its own stopping rule.
""".strip())

    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "serial_dependence.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "target_bss": args.target_bss,
            "n_eff_required": target,
            "power_coefficient": POWER_COEFFICIENT,
            "k_per_week": k,
            "r_within": rw,
            "critical_r_between": crit,
            "analytic": rows,
            "empirical_week_bootstrap": emp,
            "empirical_regime_bootstrap": fix,
        }, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Sequential testing: when you are allowed to look at the gate.

v2 §12 says "predeclare evaluation dates or use a justified sequential-testing
procedure. Repeatedly checking a 95% interval and stopping at the first
favourable result is not a gate, it is p-hacking." That was stated and never
implemented, which is the usual fate of such sentences.

The temptation is structural, not a lapse of discipline: forecasts accumulate
continuously, the gate is cheap to evaluate, and there is always a reason to
check whether the record is good enough yet. A rule that depends on people not
looking will fail. So the looking is budgeted instead.

`phase1/sequential_peeking.py` measures what unbudgeted looking costs. This
module implements the budget:

  * **Pocock** — a constant, more demanding threshold at every look. Spends the
    budget evenly. Good when an early stop is genuinely valuable.
  * **O'Brien-Fleming** — very demanding early, approaching the nominal level at
    the final look. Preserves almost all power for the end of the study and makes
    early stopping rare but meaningful. The default, because this project's cost
    of a false "it works" vastly exceeds the cost of running the full schedule.

Both are implemented as alpha-spending functions over information fraction, so
the schedule does not have to be fixed in advance — only the maximum number of
looks and the total budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


class SequentialError(RuntimeError):
    """The testing schedule was misused."""


@dataclass(frozen=True)
class Look:
    index: int                  # 1-based
    information_fraction: float # 0 < t <= 1
    alpha_spent_cumulative: float
    alpha_this_look: float
    z_threshold: float


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation)."""
    if not 0.0 < p < 1.0:
        raise SequentialError(f"quantile out of range: {p}")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def spend_pocock(t: float, alpha: float) -> float:
    """Cumulative alpha spent by information fraction t. Even across looks."""
    if not 0.0 < t <= 1.0:
        raise SequentialError(f"information fraction must be in (0, 1], got {t}")
    return alpha * math.log(1.0 + (math.e - 1.0) * t)


def spend_obrien_fleming(t: float, alpha: float) -> float:
    """
    Cumulative alpha spent by information fraction t. Very little spent early.

    Lan-DeMets approximation: 2(1 - Phi(z_{alpha/2} / sqrt(t))).
    """
    if not 0.0 < t <= 1.0:
        raise SequentialError(f"information fraction must be in (0, 1], got {t}")
    z = _norm_ppf(1.0 - alpha / 2.0)
    return 2.0 * (1.0 - _norm_cdf(z / math.sqrt(t)))


SPENDING = {"obrien_fleming": spend_obrien_fleming, "pocock": spend_pocock}


def schedule(
    n_looks: int,
    alpha: float = 0.05,
    spending: str = "obrien_fleming",
) -> list[Look]:
    """
    Build an equally spaced testing schedule.

    `alpha` is the TOTAL budget across all looks, not per look. A schedule of 10
    looks at nominal 0.05 each would spend far more than 0.05 overall, which is
    precisely the error this module exists to prevent.
    """
    if n_looks < 1:
        raise SequentialError("need at least one look")
    if spending not in SPENDING:
        raise SequentialError(
            f"unknown spending function {spending!r}; choose from {sorted(SPENDING)}"
        )
    fn = SPENDING[spending]

    looks: list[Look] = []
    prev = 0.0
    for i in range(1, n_looks + 1):
        t = i / n_looks
        cum = fn(t, alpha)
        this = max(0.0, cum - prev)
        # One-sided threshold for the increment available at this look.
        z = _norm_ppf(1.0 - this) if this > 0 else float("inf")
        looks.append(Look(i, t, cum, this, z))
        prev = cum
    return looks


def critical_lower_bound(look: Look, std_error: float) -> float:
    """
    How far above zero the point estimate must sit at this look.

    Expressed as a lower-bound threshold so it drops straight into the existing
    gate: instead of asking `lower > 0`, ask `point - z*se > 0` with z from the
    schedule rather than a fixed 1.96.
    """
    if std_error < 0:
        raise SequentialError("standard error cannot be negative")
    return look.z_threshold * std_error


def gate_fires(point: float, std_error: float, look: Look) -> bool:
    """Whether the sequential gate passes at this look."""
    if math.isinf(look.z_threshold):
        return False
    return point - look.z_threshold * std_error > 0.0


def describe(looks: list[Look]) -> str:
    out = [f"{'look':>5}{'info':>8}{'alpha_i':>10}{'cum':>10}{'z':>8}"]
    for lk in looks:
        z = "inf" if math.isinf(lk.z_threshold) else f"{lk.z_threshold:.3f}"
        out.append(f"{lk.index:>5}{lk.information_fraction:>8.2f}"
                   f"{lk.alpha_this_look:>10.5f}{lk.alpha_spent_cumulative:>10.5f}{z:>8}")
    return "\n".join(out)

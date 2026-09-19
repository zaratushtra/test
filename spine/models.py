"""
Forecasting models: baseline, independent, market-conditioned.

Three models, deliberately kept apart because they answer different questions and
each can fail without the others noticing (v2 §5.2):

  * **Baseline** — the reference class alone, deadline-aware. The thing any
    claimed skill must beat before it is worth discussing.
  * **Independent** — baseline plus evidence, produced *without seeing the target
    price*. Measures what the evidence system knows on its own.
  * **Market-conditioned** — tests whether the evidence adds anything beyond the
    contemporaneous price, which is the only version of "edge" that matters.

Two things this module gets right that v1 did not:

**Deadlines are a hazard problem.** "Will X happen by date D" is not a static
probability that happens to have a date attached. If events arrive at rate λ over
an exposure window, then P(by D) = 1 − exp(−λ·t), and the reference class must
supply λ from *events per unit exposure*, not k/n. A static estimator gives the
same answer with three days left as with three months, which is plainly wrong and
was exactly what §5.3 objected to.

**The market-conditioned model shrinks toward the market, not toward zero.** The
correct prior for "we have no edge" is the price itself — b = 1, a = 0 — so the
ridge penalty pulls there. A model regularised toward zero would, absent
evidence, confidently predict 50% on everything.

Stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Estimates are clamped away from certainty. Not a probability cap in the v1
# sense — the whole open interval remains reachable — but 0 and 1 are not
# forecasts, they are claims of omniscience, and they break log-odds arithmetic.
EPS_BP = 1


class ModelError(RuntimeError):
    """The model could not produce a forecast from the inputs given."""


def logit(p: float) -> float:
    if not 0.0 < p < 1.0:
        raise ModelError(f"logit undefined at p={p}")
    return math.log(p / (1.0 - p))


def expit(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def to_bp(p: float) -> int:
    """Probability to basis points, clamped off the endpoints."""
    return max(EPS_BP, min(10000 - EPS_BP, round(p * 10000)))


@dataclass(frozen=True)
class Forecast:
    p_est_bp: int
    p_lo_bp: int
    p_hi_bp: int
    uncertainty_method: str
    kind: str                      # 'baseline' | 'independent' | 'market_conditioned'
    components: dict               # everything needed to reconstruct the number

    def width_bp(self) -> int:
        return self.p_hi_bp - self.p_lo_bp


# ---------------------------------------------------------------------------
# reference class -> hazard
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReferenceClass:
    """
    A frozen reference class.

    `k` events observed over `exposure_units` of exposure — country-years,
    case-weeks, whatever the family's natural unit is. `n` is retained for the
    static case but the hazard path uses exposure, which is the whole point of
    §5.3: a class of 40 cases where 4 resolved says nothing until you know
    whether that was over 40 weeks or 40 years.
    """
    name: str
    version: int
    k: int
    n: int
    exposure_units: float
    alpha: float
    beta: float

    def static_rate(self) -> float:
        """Beta-smoothed event probability, ignoring time. Use only for fixed-window events."""
        return (self.k + self.alpha) / (self.n + self.alpha + self.beta)

    def hazard(self) -> float:
        """Events per unit exposure, Beta-smoothed. The deadline-aware quantity."""
        if self.exposure_units <= 0:
            raise ModelError(f"{self.name}: exposure_units must be positive for a hazard")
        return (self.k + self.alpha) / (self.exposure_units + self.alpha + self.beta)

    def hazard_interval(self, z: float = 1.96) -> tuple[float, float]:
        """
        Crude interval on the hazard from the Beta posterior's variance.

        Deliberately crude and labelled as such: the honest uncertainty comes
        from the regime-level bootstrap once forecasts resolve. This is a prior
        width, not a measured one.
        """
        a = self.k + self.alpha
        b = self.exposure_units + self.alpha + self.beta
        mean = a / b
        sd = math.sqrt(a) / b
        return max(1e-9, mean - z * sd), mean + z * sd


def hazard_to_probability(rate: float, time_remaining: float) -> float:
    """
    P(at least one event in the remaining window) under a constant hazard.

        P = 1 − exp(−rate · t)

    Constant hazard is an assumption, not a law. It is right for "has a scheduled
    thing happened yet" and wrong where risk concentrates near the deadline; the
    family's own model should replace it where that matters.
    """
    if rate < 0:
        raise ModelError("hazard rate cannot be negative")
    if time_remaining < 0:
        raise ModelError("time remaining cannot be negative")
    return 1.0 - math.exp(-rate * time_remaining)


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------

def baseline_forecast(
    rc: ReferenceClass,
    time_remaining: float | None = None,
    deadline_aware: bool = True,
) -> Forecast:
    """
    The reference class alone. Everything else must beat this.

    With `deadline_aware`, the probability decays as the window closes — which is
    the behaviour §5.3 required and a static rate cannot produce.
    """
    if deadline_aware:
        if time_remaining is None:
            raise ModelError("deadline_aware baseline needs time_remaining")
        rate = rc.hazard()
        lo_rate, hi_rate = rc.hazard_interval()
        p = hazard_to_probability(rate, time_remaining)
        lo = hazard_to_probability(lo_rate, time_remaining)
        hi = hazard_to_probability(hi_rate, time_remaining)
        method = "beta_hazard_constant"
        comps = {"hazard": rate, "time_remaining": time_remaining,
                 "hazard_lo": lo_rate, "hazard_hi": hi_rate}
    else:
        p = rc.static_rate()
        a, b = rc.k + rc.alpha, rc.n - rc.k + rc.beta
        sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
        lo, hi = max(1e-6, p - 1.96 * sd), min(1 - 1e-6, p + 1.96 * sd)
        method = "beta_static"
        comps = {"static_rate": p, "posterior_sd": sd}

    comps.update({"reference_class": rc.name, "version": rc.version,
                  "k": rc.k, "n": rc.n, "exposure_units": rc.exposure_units})
    return Forecast(to_bp(p), to_bp(min(lo, p)), to_bp(max(hi, p)), method,
                    "baseline", comps)


# ---------------------------------------------------------------------------
# independent
# ---------------------------------------------------------------------------

def independent_forecast(
    base: Forecast,
    contributions: list[float],
    lambda_sig: float = 1.0,
    lambda_version: str = "unset",
    widen_per_contribution_bp: int = 25,
) -> Forecast:
    """
    Baseline updated by evidence, in log-odds. Never sees the market price.

    `contributions` are already-capped per-cluster contributions from
    `claim_contract_effects.final_contribution` — the influence budget binds
    before this point, not here (§6). Summing raw likelihood ratios would
    reintroduce exactly the ceiling v1 claimed and did not have.

    The interval *widens* with each contribution applied. Evidence moves the
    estimate and adds model risk at the same time; a forecast that got more
    confident purely because more news arrived would be counting corroboration
    as certainty.
    """
    if lambda_sig < 0:
        raise ModelError("lambda_sig cannot be negative")

    p0 = base.p_est_bp / 10000.0
    shift = lambda_sig * sum(contributions)
    p = expit(logit(p0) + shift)

    lo0, hi0 = base.p_lo_bp / 10000.0, base.p_hi_bp / 10000.0
    lo = expit(logit(lo0) + shift)
    hi = expit(logit(hi0) + shift)
    widen = widen_per_contribution_bp * len(contributions)

    return Forecast(
        to_bp(p),
        max(EPS_BP, to_bp(lo) - widen),
        min(10000 - EPS_BP, to_bp(hi) + widen),
        "baseline_interval_shifted_plus_model_risk",
        "independent",
        {"base_p_bp": base.p_est_bp, "n_contributions": len(contributions),
         "total_shift_logodds": shift, "lambda_sig": lambda_sig,
         "lambda_version": lambda_version,
         "contributions": list(contributions)},
    )


# ---------------------------------------------------------------------------
# market-conditioned
# ---------------------------------------------------------------------------

def _solve2(a11, a12, a21, a22, b1, b2) -> tuple[float, float]:
    det = a11 * a22 - a12 * a21
    if abs(det) < 1e-12:
        raise ModelError("singular normal equations; not enough variation to fit")
    return (b1 * a22 - a12 * b2) / det, (a11 * b2 - b1 * a21) / det


def fit_market_conditioned(
    market_p: list[float],
    outcomes: list[int],
    ridge: float = 10.0,
) -> tuple[float, float]:
    """
    Fit `logit(p) = a + b·logit(q)` with ridge shrinkage toward (a=0, b=1).

    Shrinking toward the *market* is the point. The null hypothesis of this
    project is "the price is already right", so with no data the fit must return
    the price unchanged. A conventional ridge toward zero would instead return
    50% for everything, which is not a humble prior — it is a confident and wrong
    one.

    `ridge` is deliberately large by default: with a few dozen observations the
    honest answer is close to "defer to the market".
    """
    if len(market_p) != len(outcomes):
        raise ModelError("market_p and outcomes must be the same length")
    if not market_p:
        raise ModelError("no observations to fit")

    xs = [logit(min(0.9999, max(0.0001, q))) for q in market_p]
    # Linearised least squares on the logit scale against the observed outcome,
    # mapped off the endpoints so logit(y) exists.
    ys = [logit(0.999 if o == 1 else 0.001) for o in outcomes]

    n = len(xs)
    sxx = sum(x * x for x in xs)
    sx = sum(xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sy = sum(ys)

    # Penalty pulls a toward 0 and b toward 1.
    a11, a12 = n + ridge, sx
    a21, a22 = sx, sxx + ridge
    b1, b2 = sy, sxy + ridge * 1.0
    return _solve2(a11, a12, a21, a22, b1, b2)


def market_conditioned_forecast(
    market_p: float,
    a: float,
    b: float,
    adjustment_logodds: float = 0.0,
    residual_sd_logodds: float = 0.5,
    model_version: str = "unset",
) -> Forecast:
    """
    `logit(p) = a + b·logit(q) + f(x, h)`, with the adjustment supplied by the
    family model.

    The uncertainty here is residual dispersion around the fit, so a
    market-conditioned forecast that merely echoes the price still carries an
    interval — it is a model output, not an observation.
    """
    if not 0.0 < market_p < 1.0:
        raise ModelError(f"market price must be in (0,1), got {market_p}")
    if residual_sd_logodds < 0:
        raise ModelError("residual sd cannot be negative")

    z = a + b * logit(market_p) + adjustment_logodds
    p = expit(z)
    lo = expit(z - 1.96 * residual_sd_logodds)
    hi = expit(z + 1.96 * residual_sd_logodds)

    return Forecast(
        to_bp(p), to_bp(lo), to_bp(hi),
        "logit_linear_residual_sd", "market_conditioned",
        {"market_p": market_p, "a": a, "b": b,
         "adjustment_logodds": adjustment_logodds,
         "residual_sd_logodds": residual_sd_logodds,
         "model_version": model_version,
         "departure_from_market_bp": to_bp(p) - to_bp(market_p)},
    )


def departure_from_market_bp(f: Forecast) -> int:
    """
    How far a market-conditioned forecast strays from the price.

    The quantity to watch in ablations: if it is near zero across the board, the
    evidence layer is adding nothing the price did not already contain, and that
    is the most likely scientific failure mode (§15.4).
    """
    if f.kind != "market_conditioned":
        raise ModelError("departure is only defined for market-conditioned forecasts")
    return f.components["departure_from_market_bp"]

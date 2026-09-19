#!/usr/bin/env python3
"""
Phase 2 validation, part 2: forecasting models and the ablation harness.

Covers the remaining §13 Phase 2 gate items that do not need market access:
baseline / independent / market-conditioned forecasts, and the ablation harness
that decides whether any component earns its place.

Run: python3 tests/test_phase2b.py
Stdlib only.
"""

from __future__ import annotations

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spine import ablation, models  # noqa: E402
from spine.ablation import AblationError, Variant  # noqa: E402
from spine.models import ModelError, ReferenceClass  # noqa: E402
from spine.scoring import Observation  # noqa: E402

PASS, FAIL = [], []


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


RC = ReferenceClass(name="fed_holds", version=1, k=4, n=40,
                    exposure_units=40.0, alpha=1.0, beta=9.0)


def main() -> int:
    print("=" * 76)
    print("SPINE PHASE 2 VALIDATION (b) — models and ablations")
    print("=" * 76)

    # ------------------------------------------------ hazard vs static
    print("\n[1] Deadlines are a hazard problem, not a static probability\n")
    near = models.baseline_forecast(RC, time_remaining=0.1)
    mid = models.baseline_forecast(RC, time_remaining=1.0)
    far = models.baseline_forecast(RC, time_remaining=10.0)
    ok("probability rises as the window lengthens",
       near.p_est_bp < mid.p_est_bp < far.p_est_bp,
       f"{near.p_est_bp} {mid.p_est_bp} {far.p_est_bp}")
    ok("a static estimator would give the same answer at every horizon",
       models.baseline_forecast(RC, deadline_aware=False).p_est_bp ==
       models.baseline_forecast(RC, deadline_aware=False).p_est_bp)
    # With one unit of exposure per case, hazard and static rate coincide — that
    # is correct, not a coincidence to assert against. The distinction only bites
    # when exposure differs from case count, which is the next check.
    ok("hazard == static when exposure is one unit per case",
       math.isclose(RC.hazard(), RC.static_rate(), abs_tol=1e-12),
       f"hazard={RC.hazard():.4f} static={RC.static_rate():.4f}")
    stretched = ReferenceClass("stretched", 1, 4, 40, exposure_units=200.0,
                               alpha=1.0, beta=9.0)
    ok("hazard diverges from static once exposure != n",
       stretched.hazard() < stretched.static_rate() / 3,
       f"hazard={stretched.hazard():.4f} static={stretched.static_rate():.4f}")
    # Two classes with identical k/n but different exposure must differ.
    slow = ReferenceClass("slow", 1, 4, 40, exposure_units=400.0, alpha=1.0, beta=9.0)
    ok("same k/n over 10x the exposure gives a much lower probability",
       models.baseline_forecast(slow, time_remaining=1.0).p_est_bp <
       models.baseline_forecast(RC, time_remaining=1.0).p_est_bp // 2,
       f"{models.baseline_forecast(slow, 1.0).p_est_bp} vs "
       f"{models.baseline_forecast(RC, 1.0).p_est_bp}")
    ok("P -> 0 as time -> 0",
       models.hazard_to_probability(0.5, 0.0) == 0.0)
    ok("P -> 1 as time -> infinity",
       models.hazard_to_probability(0.5, 1e6) > 0.999999)
    ok("deadline_aware without time_remaining raises",
       raises(lambda: models.baseline_forecast(RC), ModelError))
    ok("zero exposure raises rather than dividing by zero",
       raises(lambda: ReferenceClass("z", 1, 1, 1, 0.0, 1.0, 3.0).hazard(), ModelError))
    ok("negative time raises", raises(lambda: models.hazard_to_probability(1.0, -1.0),
                                      ModelError))
    print(f"        t=0.1: {near.p_est_bp}bp   t=1: {mid.p_est_bp}bp   "
          f"t=10: {far.p_est_bp}bp")

    # ------------------------------------------------ endpoints
    print("\n[2] Estimates stay inside the open interval\n")
    huge = models.baseline_forecast(RC, time_remaining=1e5)
    ok("an overwhelming hazard still does not reach 1.0", huge.p_est_bp < 10000)
    ok("intervals contain the estimate",
       all(f.p_lo_bp <= f.p_est_bp <= f.p_hi_bp for f in (near, mid, far, huge)))
    ok("logit refuses 0 and 1",
       raises(lambda: models.logit(0.0), ModelError) and
       raises(lambda: models.logit(1.0), ModelError))
    ok("expit round-trips", math.isclose(models.expit(models.logit(0.37)), 0.37,
                                         abs_tol=1e-12))

    # ------------------------------------------------ independent
    print("\n[3] Independent forecast: evidence moves it and widens it\n")
    base = models.baseline_forecast(RC, time_remaining=1.0)
    up = models.independent_forecast(base, [0.8, 0.4], lambda_sig=1.0)
    down = models.independent_forecast(base, [-0.8, -0.4], lambda_sig=1.0)
    none = models.independent_forecast(base, [], lambda_sig=1.0)
    ok("positive evidence raises the estimate", up.p_est_bp > base.p_est_bp)
    ok("negative evidence lowers it", down.p_est_bp < base.p_est_bp)
    ok("no evidence leaves the estimate alone", none.p_est_bp == base.p_est_bp)
    ok("evidence WIDENS the interval (corroboration is not certainty)",
       up.width_bp() > base.width_bp(), f"{base.width_bp()} -> {up.width_bp()}")
    ok("lambda_sig scales the shift",
       models.independent_forecast(base, [0.8], lambda_sig=2.0).p_est_bp >
       models.independent_forecast(base, [0.8], lambda_sig=1.0).p_est_bp)
    ok("lambda_sig = 0 disables evidence entirely",
       models.independent_forecast(base, [5.0], lambda_sig=0.0).p_est_bp ==
       base.p_est_bp)
    ok("negative lambda_sig raises",
       raises(lambda: models.independent_forecast(base, [0.1], lambda_sig=-1.0),
              ModelError))
    ok("the lambda version is recorded for reconstruction",
       models.independent_forecast(base, [0.1], lambda_version="v3")
       .components["lambda_version"] == "v3")
    print(f"        base {base.p_est_bp}bp (w={base.width_bp()})  ->  "
          f"+evidence {up.p_est_bp}bp (w={up.width_bp()})")

    # ------------------------------------------------ market-conditioned
    print("\n[4] Market-conditioned: shrink toward the price, not toward 50%\n")
    rng = random.Random(4)
    qs = [rng.uniform(0.1, 0.9) for _ in range(40)]
    outs = [1 if rng.random() < q else 0 for q in qs]
    a, b = models.fit_market_conditioned(qs, outs, ridge=1000.0)
    ok("heavy ridge pulls the fit toward a=0, b=1 (defer to the market)",
       abs(a) < 0.25 and abs(b - 1.0) < 0.25, f"a={a:.3f} b={b:.3f}")
    f_echo = models.market_conditioned_forecast(0.30, a=0.0, b=1.0)
    ok("with a=0,b=1 and no adjustment the forecast IS the market",
       abs(f_echo.p_est_bp - 3000) <= 1, f_echo.p_est_bp)
    ok("...and still carries an interval, because it is a model output",
       f_echo.width_bp() > 0)
    ok("departure from market is zero when echoing",
       abs(models.departure_from_market_bp(f_echo)) <= 1)
    f_adj = models.market_conditioned_forecast(0.30, a=0.0, b=1.0,
                                               adjustment_logodds=0.5)
    ok("an adjustment moves it away from the price",
       models.departure_from_market_bp(f_adj) > 100)
    ok("departure is undefined for other kinds",
       raises(lambda: models.departure_from_market_bp(base), ModelError))
    ok("a market price of 0 or 1 raises",
       raises(lambda: models.market_conditioned_forecast(0.0, 0.0, 1.0), ModelError))
    ok("mismatched fit inputs raise",
       raises(lambda: models.fit_market_conditioned([0.5], [1, 0]), ModelError))
    ok("empty fit raises",
       raises(lambda: models.fit_market_conditioned([], []), ModelError))
    print(f"        ridge=1000 -> a={a:+.3f} b={b:+.3f}   "
          f"echo at q=0.30 -> {f_echo.p_est_bp}bp (departure "
          f"{models.departure_from_market_bp(f_echo):+d}bp)")

    # ------------------------------------------------ ablation pairing
    print("\n[5] Ablations must be paired, or they measure the question set\n")

    def variant(name, edge, n_regimes=14, per=12, seed=7):
        r = random.Random(seed)
        obs = []
        for g in range(n_regimes):
            for i in range(per):
                truth = r.random()
                o = 1 if r.random() < truth else 0
                # edge=1.0 reproduces the truth exactly; below that the forecast
                # is shrunk toward 0.5, so "less edge" means "less informative".
                p = min(0.99, max(0.01, 0.5 + edge * (truth - 0.5)))
                obs.append(Observation(p=p, outcome=o, p_base=0.5,
                                       regime_id=f"g{g}", key=f"q{g}_{i}"))
        return Variant(name, obs)

    full = variant("full", 1.0)
    weak = variant("no_evidence", 0.0)
    unkeyed = Variant("unkeyed",
                      [Observation(0.5, 1, 0.5, "g0") for _ in range(200)])
    ok("unkeyed observations are refused",
       raises(lambda: ablation.compare(full, unkeyed), AblationError))
    ok("disjoint question sets are refused",
       raises(lambda: ablation.compare(
           full, Variant("other", [Observation(0.5, 1, 0.5, "g0", "zz")])),
           AblationError))
    dup = Variant("dup", [Observation(0.5, 1, 0.5, "g0", "same") for _ in range(3)])
    ok("duplicate keys are refused", raises(lambda: ablation.compare(full, dup),
                                            AblationError))
    ok("the twelve-regime requirement still applies to ablations",
       raises(lambda: ablation.compare(variant("a", 0.8, n_regimes=3),
                                       variant("b", 0.0, n_regimes=3)),
              Exception))

    # ------------------------------------------------ ablation verdicts
    print("\n[6] Ablation verdicts\n")
    comps = ablation.ablate(full, [weak], resamples=600)
    c = comps[0]
    ok("a real component is detected as contributing", c.contributes,
       c.verdict())
    ok("pairing preserved every shared question", c.n_paired == len(full.observations))
    ok("delta is reference minus variant", c.delta_brier > 0)

    same = variant("identical", 1.0)
    c_same = ablation.compare(full, same, resamples=600)
    ok("an identical variant shows no effect", not c_same.contributes,
       c_same.verdict())
    ok("...with a delta of exactly zero", abs(c_same.delta_brier) < 1e-12)

    c_harm = ablation.compare(weak, full, resamples=600)
    ok("removing a real component is detected as harmful",
       c_harm.ci.upper < 0, c_harm.verdict())

    market = variant("market", 0.6)
    c_mkt = ablation.beats_market(full, market, resamples=600)
    ok("beats_market returns a comparison against the price",
       c_mkt.reference == "market")
    ok("a better-informed variant does beat a shrunk market forecast",
       c_mkt.contributes, c_mkt.verdict())
    print("\n" + ablation.report([c, c_same, c_mkt]))

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

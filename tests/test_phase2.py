#!/usr/bin/env python3
"""
Phase 2 validation: scoring and decision layers.

Gate items from SPINE-DESIGN-V2 §13 Phase 2, plus the one added after Phase 1:
the regime-level bootstrap must be the ONLY scoring path, so the invalid
week-level rule cannot be used by accident.

Run: python3 tests/test_phase2.py
Stdlib only.
"""

from __future__ import annotations

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spine import decision, scoring, sequential  # noqa: E402
from spine.decision import BookLevel  # noqa: E402
from spine.scoring import Observation, ScoringError  # noqa: E402

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


def obs(p, outcome, base=0.5, regime="r1"):
    return Observation(p=p, outcome=outcome, p_base=base, regime_id=regime)


def synth(n_regimes, per_regime, edge=0.0, seed=1):
    """Forecasts with a controllable edge over a 0.5 benchmark."""
    rng = random.Random(seed)
    out = []
    for r in range(n_regimes):
        for _ in range(per_regime):
            truth = rng.random()
            o = 1 if rng.random() < truth else 0
            p = min(0.99, max(0.01, 0.5 + edge * (truth - 0.5) * 2))
            out.append(obs(p, o, 0.5, f"reg{r}"))
    return out


def main() -> int:
    print("=" * 74)
    print("SPINE PHASE 2 VALIDATION — scoring and decision")
    print("=" * 74)

    # ---------------------------------------------------------- scoring basics
    print("\n[1] Brier and skill\n")
    perfect = [obs(1.0, 1), obs(0.0, 0)]
    ok("perfect forecasts score 0", scoring.brier(perfect) == 0.0)
    worst = [obs(0.0, 1), obs(1.0, 0)]
    ok("maximally wrong forecasts score 1", scoring.brier(worst) == 1.0)
    ok("a 5% forecast on a 5% event beats a capped 40% one",
       scoring.brier([obs(0.05, 0)]) < scoring.brier([obs(0.40, 0)]))
    ok("BSS positive when the model beats the benchmark",
       scoring.bss([obs(0.9, 1, base=0.5), obs(0.1, 0, base=0.5)]) > 0)
    ok("BSS negative when the model loses to the benchmark",
       scoring.bss([obs(0.1, 1, base=0.5), obs(0.9, 0, base=0.5)]) < 0)
    ok("empty input raises rather than returning a number",
       raises(lambda: scoring.brier([]), ScoringError))

    print("\n[2] Unscorable outcomes are excluded, not counted as misses\n")
    rows = [
        {"p_est_bp": 9000, "p_base_bp": 5000, "regime_id": "r1", "outcome": "resolved_yes"},
        {"p_est_bp": 9000, "p_base_bp": 5000, "regime_id": "r1", "outcome": "void"},
        {"p_est_bp": 9000, "p_base_bp": 5000, "regime_id": "r1", "outcome": "disputed"},
        {"p_est_bp": 9000, "p_base_bp": 5000, "regime_id": "r1", "outcome": "unresolvable"},
    ]
    scored = scoring.to_observations(rows)
    ok("only the resolved outcome is scored", len(scored) == 1)
    ok("a void does not become a miss", scoring.brier(scored) == (0.9 - 1) ** 2)

    # ---------------------------------------------------------- murphy
    print("\n[3] Murphy decomposition — skill is not calibration\n")
    rng = random.Random(3)
    calibrated = [obs(p := round(rng.random(), 2), 1 if rng.random() < p else 0)
                  for _ in range(4000)]
    m = scoring.murphy(calibrated)
    # The classical identity is exact only for discrete forecast values. With
    # continuous forecasts the slack is the within-bin variance, and it must
    # shrink toward zero as bins approach the number of distinct values.
    discrete = [obs(p_, 1 if rng.random() < p_ else 0)
                for p_ in (rng.choice([0.1, 0.3, 0.5, 0.7, 0.9])
                           for _ in range(4000))]
    ok("identity is exact for discrete forecasts",
       abs(scoring.murphy(discrete, bins=10)["within_bin_residual"]) < 1e-12,
       scoring.murphy(discrete, bins=10)["within_bin_residual"])
    residuals = [abs(scoring.murphy(calibrated, bins=b)["within_bin_residual"])
                 for b in (5, 20, 101)]
    ok("residual shrinks as bins approach distinct values",
       residuals[0] > residuals[1] > residuals[2] and residuals[2] < 1e-9,
       residuals)
    ok("a calibrated forecaster has low reliability error",
       m["reliability"] < 0.01, m["reliability"])

    # Overconfident: push every forecast away from 0.5 without changing ranking.
    over = [obs(min(0.99, max(0.01, 0.5 + (o.p - 0.5) * 1.8)), o.outcome)
            for o in calibrated]
    m_over = scoring.murphy(over)
    ok("an overconfident forecaster shows worse reliability",
       m_over["reliability"] > m["reliability"] * 3,
       f"{m['reliability']:.5f} -> {m_over['reliability']:.5f}")
    ok("...while keeping comparable resolution (skill != calibration)",
       abs(m_over["resolution"] - m["resolution"]) < m["resolution"] * 0.5)
    print(f"        calibrated : rel={m['reliability']:.5f} res={m['resolution']:.4f}")
    print(f"        overconfident: rel={m_over['reliability']:.5f} "
          f"res={m_over['resolution']:.4f}")

    ok("calibration curve bins and reports observed rates",
       len(scoring.calibration_curve(calibrated)) >= 8)

    # ------------------------------------------- the invalid path is unavailable
    print("\n[4] The invalid scoring path cannot be used by accident\n")
    ok("no week-level bootstrap is exported",
       not any(n for n in dir(scoring) if "week" in n.lower()))
    ok("1 regime is refused",
       raises(lambda: scoring.regime_bootstrap_ci(synth(1, 200)), ScoringError))
    ok("4 regimes are refused (Phase 1 measured 13% false positives there)",
       raises(lambda: scoring.regime_bootstrap_ci(synth(4, 50)), ScoringError))
    ok("11 regimes are refused",
       raises(lambda: scoring.regime_bootstrap_ci(synth(11, 20)), ScoringError))
    ok("12 regimes are accepted",
       isinstance(scoring.regime_bootstrap_ci(synth(12, 20), resamples=400),
                  scoring.BootstrapResult))
    try:
        scoring.regime_bootstrap_ci(synth(1, 200))
    except ScoringError as e:
        ok("the refusal explains itself and says what to do",
           "43.5%" in str(e) and "Rotate" in str(e))

    # ---------------------------------------------------------- gate behaviour
    print("\n[5] Gate behaviour on real edge vs no edge\n")
    no_edge = synth(16, 40, edge=0.0, seed=11)
    r_none = scoring.regime_bootstrap_ci(no_edge, resamples=800, seed=5)
    ok("no edge: gate does not fire", not r_none.fires,
       f"lower={r_none.lower:.4f}")
    big_edge = synth(16, 40, edge=0.95, seed=11)
    r_big = scoring.regime_bootstrap_ci(big_edge, resamples=800, seed=5)
    ok("large edge: gate fires", r_big.fires, f"lower={r_big.lower:.4f}")
    ok("interval brackets the point estimate",
       r_big.lower <= r_big.point <= r_big.upper)
    print(f"        no edge  : point={r_none.point:+.4f} "
          f"[{r_none.lower:+.4f}, {r_none.upper:+.4f}]")
    print(f"        big edge : point={r_big.point:+.4f} "
          f"[{r_big.lower:+.4f}, {r_big.upper:+.4f}]")

    # ---------------------------------------------------------- r_between
    print("\n[6] r_between is measured, not assumed\n")
    ok("needs at least 2 regimes",
       raises(lambda: scoring.estimate_r_between(synth(1, 50)), ScoringError))
    indep = synth(20, 30, edge=0.0, seed=2)
    r_hat = scoring.estimate_r_between(indep)
    ok("independent regimes estimate near zero", r_hat < 0.05, f"{r_hat:.4f}")
    # Inject a regime-level shift: the estimator must notice.
    shifted = []
    rng2 = random.Random(9)
    for i in range(20):
        bump = rng2.gauss(0, 0.30)
        for _ in range(30):
            o = 1 if rng2.random() < 0.5 else 0
            shifted.append(obs(min(0.99, max(0.01, 0.5 + bump)), o, 0.5, f"reg{i}"))
    r_shift = scoring.estimate_r_between(shifted)
    ok("a shared per-regime component is detected", r_shift > r_hat,
       f"{r_hat:.4f} -> {r_shift:.4f}")
    ok("the ceiling follows from the estimate",
       math.isclose(scoring.n_eff_ceiling(0.05), 20.0))
    print(f"        independent r_between={r_hat:.4f} -> ceiling "
          f"{scoring.n_eff_ceiling(r_hat):.0f}")
    print(f"        shifted     r_between={r_shift:.4f} -> ceiling "
          f"{scoring.n_eff_ceiling(r_shift):.0f}")

    # ---------------------------------------------------------- book walking
    print("\n[7] Executable price, not midpoint\n")
    book = [BookLevel(6000, 50.0), BookLevel(6200, 50.0), BookLevel(6500, 100.0)]
    f = decision.walk_book(book, 50.0)
    ok("small size fills at the best level", f.expected_price_bp == 6000.0)
    f2 = decision.walk_book(book, 100.0)
    ok("larger size pays a worse average", math.isclose(f2.expected_price_bp, 6100.0))
    f3 = decision.walk_book(book, 300.0)
    ok("depth shortfall is reported, not priced as if available",
       not f3.fully_filled and f3.filled_usd == 200.0)
    ok("empty book fills nothing",
       decision.walk_book([], 10.0).filled_usd == 0.0)
    print(f"        50 USD -> {f.expected_price_bp:.0f}bp | "
          f"100 USD -> {f2.expected_price_bp:.0f}bp  (midpoint would flatter both)")

    # ---------------------------------------------------------- decisions
    print("\n[8] Decisions abstain by default and explain themselves\n")
    common = dict(
        p_est_bp=6400, p_lo_bp=6000, p_hi_bp=6800, side="YES",
        book=[BookLevel(6100, 500.0)], intended_size_usd=100.0, costs_bp=100.0,
        eligibility_status="tradeable", max_notional_usd=200.0,
        cluster_exposure_used_usd=0.0, cluster_exposure_cap_usd=500.0,
    )

    # The worked example from v2 section 10.2: 64% central, 60% conservative,
    # 61c executable, 1c costs -> -2c, abstain.
    d = decision.decide(**common)
    ok("the §10.2 worked example abstains", not d.permitted)
    ok("...at -200bp as documented", d.ev_per_share_bp == -200.0, d.ev_per_share_bp)
    ok("...and notes that the point estimate alone would have traded",
       any("point estimate" in n for n in d.notes), d.notes)
    print(f"        {d.abstain_reason}")
    print(f"        note: {d.notes[0] if d.notes else '-'}")

    ok("LIVE mode: close-only abstains regardless of edge",
       not decision.decide(**{**common, "p_lo_bp": 9000, "p_est_bp": 9200,
                              "p_hi_bp": 9400, "eligibility_status": "close_only",
                              "mode": decision.LIVE}).permitted)
    ok("LIVE mode: unknown eligibility abstains",
       not decision.decide(**{**common, "eligibility_status": "unknown",
                              "mode": decision.LIVE}).permitted)
    ok("size over max notional abstains",
       not decision.decide(**{**common, "intended_size_usd": 500.0}).permitted)
    ok("cluster exposure headroom is enforced",
       not decision.decide(**{**common, "cluster_exposure_used_usd": 450.0}).permitted)
    ok("insufficient depth abstains",
       not decision.decide(**{**common, "book": [BookLevel(6100, 10.0)]}).permitted)
    # Paper mode is the posture under section 2.1: the venue is close-only from
    # our jurisdiction, so enforcing eligibility would abstain on everything and
    # the decision layer would measure nothing.
    paper_co = decision.decide(**{**common, "p_lo_bp": 7500, "p_est_bp": 7800,
                                  "p_hi_bp": 8100, "eligibility_status": "close_only"})
    ok("PAPER mode: close-only still evaluates the counterfactual",
       paper_co.permitted)
    ok("...and marks itself a simulation", paper_co.is_simulation)
    ok("...and says so in the notes, so it cannot be mistaken for permission",
       any("SIMULATED" in n and "confers no permission" in n for n in paper_co.notes),
       paper_co.notes)
    ok("paper is the default mode",
       decision.decide(**common).mode == decision.PAPER)
    ok("an invalid mode raises",
       raises(lambda: decision.decide(**{**common, "mode": "yolo"}),
              decision.DecisionError))
    ok("non-eligibility abstentions still bind in PAPER mode",
       not decision.decide(**{**common, "intended_size_usd": 500.0}).permitted)
    print(f"        paper/close-only: permitted={paper_co.permitted} "
          f"simulation={paper_co.is_simulation}")

    ok("every abstention carries a reason",
       all(decision.decide(**{**common, k: v, "mode": decision.LIVE}).abstain_reason
           for k, v in [("eligibility_status", "blocked"),
                        ("intended_size_usd", 500.0),
                        ("book", [])]))

    good = decision.decide(**{**common, "p_lo_bp": 7500, "p_est_bp": 7800,
                              "p_hi_bp": 8100})
    ok("a genuine edge is permitted", good.permitted)
    ok("...and records the executable price, not the midpoint",
       good.expected_acquisition_bp == 6100.0)
    ok("...and uses the conservative end of the interval",
       good.conservative_p_bp == 7500)
    ok("...with EV = 7500 - 6100 - 100", good.ev_per_share_bp == 1300.0)
    print(f"        permitted: acq={good.expected_acquisition_bp:.0f}bp "
          f"conservative={good.conservative_p_bp}bp ev={good.ev_per_share_bp:.0f}bp")

    print("\n[9] NO-side uses the opposite interval end\n")
    no_side = dict(common, side="NO", p_est_bp=3600, p_lo_bp=3200, p_hi_bp=4000,
                   book=[BookLevel(5000, 500.0)])
    d_no = decision.decide(**no_side)
    ok("NO uses the HIGH end as conservative",
       decision.conservative_probability_bp(3600, 3200, 4000, "NO") == 4000)
    ok("NO settlement value is 1 - p", d_no.ev_per_share_bp == (10000 - 4000) - 5000 - 100)
    ok("an invalid side raises",
       raises(lambda: decision.conservative_probability_bp(1, 1, 1, "MAYBE"),
              decision.DecisionError))

    # ------------------------------------------------- sequential testing
    print("\n[10] Looking at the gate is budgeted, not free\n")
    sched = sequential.schedule(20, alpha=0.05, spending="obrien_fleming")
    ok("schedule has one entry per look", len(sched) == 20)
    ok("cumulative alpha reaches exactly the budget",
       math.isclose(sched[-1].alpha_spent_cumulative, 0.05, abs_tol=1e-9),
       sched[-1].alpha_spent_cumulative)
    ok("per-look increments sum to the budget",
       math.isclose(sum(lk.alpha_this_look for lk in sched), 0.05, abs_tol=1e-9))
    ok("cumulative spend is monotone",
       all(a.alpha_spent_cumulative <= b.alpha_spent_cumulative
           for a, b in zip(sched, sched[1:])))
    ok("information fraction ends at 1.0", sched[-1].information_fraction == 1.0)

    poc = sequential.schedule(20, alpha=0.05, spending="pocock")
    ok("O'Brien-Fleming spends far less than Pocock at the first look",
       sched[2].alpha_spent_cumulative < poc[2].alpha_spent_cumulative / 10,
       f"OBF={sched[2].alpha_spent_cumulative:.2e} Pocock={poc[2].alpha_spent_cumulative:.2e}")
    ok("O'Brien-Fleming keeps more budget for the final look",
       sched[-1].alpha_this_look > poc[-1].alpha_this_look)
    ok("every sequential threshold is stricter than the nominal 1.645",
       all(lk.z_threshold > 1.645 for lk in sched))

    ok("an early look with a modest estimate does not fire",
       not sequential.gate_fires(0.20, 0.10, sched[3]))
    ok("the same estimate at the final look does fire",
       sequential.gate_fires(0.20, 0.05, sched[-1]))
    ok("a zero estimate never fires",
       not any(sequential.gate_fires(0.0, 0.01, lk) for lk in sched))
    ok("unknown spending function raises",
       raises(lambda: sequential.schedule(5, spending="made_up"),
              sequential.SequentialError))
    ok("zero looks raises",
       raises(lambda: sequential.schedule(0), sequential.SequentialError))
    ok("information fraction outside (0,1] raises",
       raises(lambda: sequential.spend_pocock(1.5, 0.05),
              sequential.SequentialError))
    print(f"        OBF look 4/20: z={sched[3].z_threshold:.2f}  "
          f"final: z={sched[-1].z_threshold:.2f}  (nominal one-sided: 1.645)")

    print("\n" + "=" * 74)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

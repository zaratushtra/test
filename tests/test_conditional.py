#!/usr/bin/env python3
"""
§5.4 and §5.5: arithmetic v1 got wrong, and arithmetic v1 never justified.

Run: python3 tests/test_conditional.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from spine import conditional, evaluate, ledger  # noqa: E402
from spine.conditional import ConditionalError  # noqa: E402

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


def main() -> int:
    print("=" * 76)
    print("SPINE CONDITIONAL PROBABILITY (§5.4, §5.5)")
    print("=" * 76)

    print("\n[1] The term v1 dropped, and which way it biases\n")
    full = conditional.marginal(0.8, 0.5, 0.2)
    v1 = 0.8 * 0.5
    ok("both branches are counted", full == 0.5, full)
    ok("v1's form gives a smaller number", v1 < full, (v1, full))
    ok("...and it is smaller by exactly the dropped branch",
       abs(full - v1 - 0.2 * 0.5) < 1e-12)
    ok("the bias is one-directional, never noise",
       all(conditional.marginal(0.8, pa, 0.2) >= 0.8 * pa
           for pa in (0.1, 0.3, 0.5, 0.7, 0.9)))
    ok("...so a dropped branch always understates",
       conditional.marginal(0.9, 0.2, 0.4) > 0.9 * 0.2)
    ok("and the wrong answer is still in [0,1], which is why it survived",
       0.0 <= v1 <= 1.0)
    print(f"        P(B|A)=0.8, P(A)=0.5, P(B|¬A)=0.2 -> {full}; "
          f"v1's form gives {v1}")

    print("\n[2] The complement has no default\n")
    ok("marginal requires all three arguments",
       raises(lambda: conditional.marginal(0.8, 0.5), TypeError))
    ok("...because a default of zero would make v1's error the easy path", True)
    ok("exclusive() states the assumption instead of hiding it",
       conditional.exclusive(0.8, 0.5) == 0.4)
    ok("...and is exactly marginal with a zero complement",
       conditional.exclusive(0.8, 0.5) == conditional.marginal(0.8, 0.5, 0.0))
    for bad in (-0.1, 1.1, float("nan")):
        ok(f"{bad} is refused as a probability",
           raises(lambda b=bad: conditional.marginal(b, 0.5, 0.1),
                  ConditionalError))

    print("\n[3] Recovering a conditional, and refusing when it is not there\n")
    ok("the inverse round-trips",
       abs(conditional.conditional_from_marginals(0.5, 0.5, 0.2) - 0.8) < 1e-12)
    ok("P(A)=1 leaves P(B|A) unidentified",
       raises(lambda: conditional.conditional_from_marginals(0.5, 1.0, 0.2),
              ConditionalError))
    ok("P(A)=0 likewise",
       raises(lambda: conditional.conditional_from_marginals(0.5, 0.0, 0.2),
              ConditionalError))
    ok("...and says the data are silent about the other branch",
       "say nothing about the other branch" in _msg(
           lambda: conditional.conditional_from_marginals(0.5, 1.0, 0.2)))
    ok("mutually inconsistent inputs are refused, not clamped",
       raises(lambda: conditional.conditional_from_marginals(0.05, 0.1, 0.9),
              ConditionalError))
    ok("...and named as inconsistent rather than uncertain",
       "mutually inconsistent" in _msg(
           lambda: conditional.conditional_from_marginals(0.05, 0.1, 0.9)))

    print("\n[4] The stored conditionals, finally read\n")
    con = ledger.connect(":memory:", create=True)
    from tests_support import build_two_forecasts  # noqa: E402
    a, b = build_two_forecasts(con)
    evaluate.link(con, a, b, conditional_prob=0.75, dependence_kind="evidence",
                  correlation=0.4, created_at="2026-09-20T08:00:00.000Z")
    out = conditional.marginal_over_edges(con, a, p_parent=0.4,
                                          p_child_given_not_parent=0.1)
    ok("a child's marginal is computed from the stored conditional",
       abs(out[b] - (0.75 * 0.4 + 0.1 * 0.6)) < 1e-12, out)
    ok("a forecast with no edges yields nothing",
       conditional.marginal_over_edges(con, b, 0.4, 0.1) == {})
    ok("the dependence kind is respected",
       conditional.marginal_over_edges(con, a, 0.4, 0.1,
                                       dependence_kind="loss") == {})

    print("\n[5] §5.5: a made-up constant that cannot hide\n")
    band = conditional.coordination_penalty(0.6, n_actors=4)
    ok("the return type has no bare adjusted probability",
       not hasattr(band, "p") and not hasattr(band, "adjusted"))
    ok("the centre uses the v1 constant", abs(band.p_centre - 0.6 * 0.7 ** 3) < 1e-12)
    ok("a band comes with it", band.p_low < band.p_centre < band.p_high)
    ok("the summary labels it subjective", "SUBJECTIVE" in band.summary())
    ok("...naming both the form and the constant as made up",
       "made-up form" in band.summary() and "made-up constant" in band.summary())
    ok("the band is wide enough to matter", band.spread > band.p_centre,
       (band.spread, band.p_centre))
    ok("a decision at 0.25 turns on the choice of rho",
       band.decision_sensitive(0.25), band.summary())
    ok("a decision at 0.6 does not", not band.decision_sensitive(0.6))
    ok("one actor means no penalty at all",
       conditional.coordination_penalty(0.6, 1).p_centre == 0.6)
    ok("more actors means a smaller centre",
       conditional.coordination_penalty(0.6, 6).p_centre
       < conditional.coordination_penalty(0.6, 3).p_centre)
    # The absolute spread is the wrong measure and says so: it peaks around
    # four actors and then NARROWS, because both ends collapse toward zero. The
    # ratio is what grows, and it grows without bound.
    ok("the absolute spread does not grow monotonically, which would mislead",
       conditional.coordination_penalty(0.6, 10).spread
       < conditional.coordination_penalty(0.6, 4).spread)
    ok("...while the ratio does, because the exponent amplifies the guess",
       conditional.coordination_penalty(0.6, 10).spread_ratio
       > conditional.coordination_penalty(0.6, 4).spread_ratio
       > conditional.coordination_penalty(0.6, 2).spread_ratio)
    ok("at ten actors the choice of rho moves the answer ~200-fold",
       conditional.coordination_penalty(0.6, 10).spread_ratio > 100,
       conditional.coordination_penalty(0.6, 10).spread_ratio)
    ok("zero actors is refused",
       raises(lambda: conditional.coordination_penalty(0.6, 0), ConditionalError))
    ok("a rho outside (0,1] is refused",
       raises(lambda: conditional.coordination_penalty(0.6, 3, rho_centre=1.5),
              ConditionalError))
    ok("a centre outside its own range is refused",
       raises(lambda: conditional.coordination_penalty(
           0.6, 3, rho_centre=0.95, rho_range=(0.5, 0.9)), ConditionalError))
    ok("...because the band would not contain the estimate",
       "would not contain the estimate" in _msg(
           lambda: conditional.coordination_penalty(
               0.6, 3, rho_centre=0.95, rho_range=(0.5, 0.9))))
    print(f"        {band.summary()}")
    print(f"        the band spans {band.spread_ratio:.1f}x — the adjustment "
          "carries more than the estimate does")
    for n in (2, 4, 10):
        bn = conditional.coordination_penalty(0.6, n)
        print(f"        n={n:>2}  {bn.p_centre:.4f}  "
              f"[{bn.p_low:.4f}, {bn.p_high:.4f}]  {bn.spread_ratio:>6.1f}x  "
              f"(absolute spread {bn.spread:.4f})")

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


if __name__ == "__main__":
    sys.exit(main())

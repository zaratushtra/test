#!/usr/bin/env python3
"""
Position sizing (§11.2): loss scenarios, liquidity, uncertainty, concentration.

And not `n_eff`, which is the whole point of the section — v1 sized on cluster
effective sample size, conflating statistical evidence with financial risk.

Run: python3 tests/test_sizing.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))  # tests_support


from spine import evaluate, ledger, sizing  # noqa: E402
from spine.decision import BookLevel  # noqa: E402
from spine.shadow import Book  # noqa: E402
from spine.sizing import SizingError  # noqa: E402

PASS, FAIL = [], []
T = "2026-09-20T08:00:00.000Z"


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


def book(bids, asks):
    return Book(1, 1, [BookLevel(p, s) for p, s in bids],
                [BookLevel(p, s) for p, s in asks], T, T)


DEEP = book([(5900, 400.0), (5800, 900.0), (5500, 3000.0)],
            [(6100, 300.0), (6200, 700.0), (6500, 2000.0)])


def main() -> int:
    print("=" * 76)
    print("SPINE POSITION SIZING (§11.2)")
    print("=" * 76)

    print("\n[1] Liquidity is measured on the side you would EXIT through\n")
    # A YES position is closed by selling into the bids.
    yes_depth = sizing.exitable_depth_usd(DEEP, "YES")
    no_depth = sizing.exitable_depth_usd(DEEP, "NO")
    ok("a YES position is sized against bid depth",
       yes_depth == 400.0 + 900.0, yes_depth)
    ok("...counting only what rests within the exit band",
       5500 < 5900 - sizing.EXIT_BAND_BP and yes_depth == 1300.0)
    ok("a NO position is sized against ask depth",
       no_depth == 300.0 + 700.0, no_depth)
    ok("...so the two differ, because the exits differ", yes_depth != no_depth)
    ok("a wider band admits more depth",
       sizing.exitable_depth_usd(DEEP, "YES", band_bp=500) == 4300.0)
    ok("a one-sided book offers no exit",
       sizing.exitable_depth_usd(book([], [(6100, 500.0)]), "YES") == 0.0)
    ok("an unknown side is refused",
       raises(lambda: sizing.exitable_depth_usd(DEEP, "EITHER"), SizingError))
    print(f"        YES exits into ${yes_depth:.0f} of bids; "
          f"NO into ${no_depth:.0f} of asks")

    print("\n[2] Uncertainty shrinks a position and never grows one\n")
    ok("a reference-width interval is undiscounted",
       sizing.uncertainty_discount(4000, 6000) == 1.0)
    ok("a narrow interval is ALSO undiscounted, not rewarded",
       sizing.uncertainty_discount(4900, 5100) == 1.0)
    wide = sizing.uncertainty_discount(2000, 8000)
    ok("a wide interval shrinks the position", 0 < wide < 1.0, wide)
    ok("a maximally wide interval goes to zero",
       sizing.uncertainty_discount(1, 9999) < 0.01,
       sizing.uncertainty_discount(1, 9999))
    ok("the discount is monotone in width",
       sizing.uncertainty_discount(3000, 7000)
       > sizing.uncertainty_discount(2000, 8000))
    ok("an inverted interval is refused",
       raises(lambda: sizing.uncertainty_discount(6000, 4000), SizingError))
    print(f"        2000bp wide -> x1.00    6000bp wide -> x{wide:.2f}")

    print("\n[3] The binding constraint is reported, not just the number\n")
    s1 = sizing.position_limit(loss_budget_usd=100.0, book=DEEP, side="YES",
                               p_lo_bp=4500, p_hi_bp=5500)
    ok("a small budget against a deep book is bound by the budget",
       s1.binding == "loss budget" and s1.max_notional_usd == 100.0,
       s1.summary())
    s2 = sizing.position_limit(loss_budget_usd=10_000.0, book=DEEP, side="YES",
                               p_lo_bp=4500, p_hi_bp=5500)
    ok("a large budget against a thin book is bound by liquidity",
       s2.binding == "liquidity", s2.summary())
    ok("...at a share of exitable depth, not all of it",
       s2.max_notional_usd == yes_depth * sizing.DEPTH_SHARE, s2.max_notional_usd)
    s3 = sizing.position_limit(loss_budget_usd=100.0, book=DEEP, side="YES",
                               p_lo_bp=1000, p_hi_bp=9000)
    ok("a wide interval cuts the size further",
       s3.max_notional_usd < s1.max_notional_usd, (s3.max_notional_usd,
                                                   s1.max_notional_usd))
    ok("...and says so", "uncertainty" in s3.binding, s3.binding)
    ok("...with the reason in the notes",
       any("interval" in n for n in s3.notes), s3.notes)
    s4 = sizing.position_limit(loss_budget_usd=100.0,
                               book=book([], [(6100, 500.0)]), side="YES",
                               p_lo_bp=4500, p_hi_bp=5500)
    ok("no exit liquidity means no position", s4.max_notional_usd == 0.0)
    ok("...and the note says a position here could not be closed",
       any("opened and not closed" in n for n in s4.notes), s4.notes)
    ok("a non-positive loss budget is refused",
       raises(lambda: sizing.position_limit(
           loss_budget_usd=0.0, book=DEEP, side="YES", p_lo_bp=4000,
           p_hi_bp=6000), SizingError))
    ok("a depth share above 1 is refused",
       raises(lambda: sizing.position_limit(
           loss_budget_usd=100.0, book=DEEP, side="YES", p_lo_bp=4000,
           p_hi_bp=6000, depth_share=1.5), SizingError))
    for s in (s1, s2, s3):
        print(f"        {s.summary()}")

    print("\n[4] Concentration is driven by LOSS covariance, not score\n")
    ok("a fully correlated group of four gets one budget",
       sizing.concentration_cap_usd(100.0, n_linked=3, correlation=1.0) == 100.0)
    ok("an independent group of four gets four budgets",
       sizing.concentration_cap_usd(100.0, n_linked=3, correlation=0.0) == 400.0)
    ok("a partly correlated group sits between",
       100.0 < sizing.concentration_cap_usd(100.0, 3, 0.5) < 400.0,
       sizing.concentration_cap_usd(100.0, 3, 0.5))
    ok("a lone position gets exactly its budget",
       sizing.concentration_cap_usd(100.0, n_linked=0) == 100.0)
    ok("negative correlation is treated as independent, not as a credit",
       sizing.concentration_cap_usd(100.0, 3, -0.9)
       == sizing.concentration_cap_usd(100.0, 3, 0.0))
    ok("a correlation outside [-1,1] is refused",
       raises(lambda: sizing.concentration_cap_usd(100.0, 1, 1.5), SizingError))

    print("\n[5] The loss graph, not the score graph\n")
    con = ledger.connect(":memory:", create=True)
    from tests_support import build_two_forecasts  # noqa: E402
    a, b = build_two_forecasts(con)
    evaluate.link(con, a, b, conditional_prob=0.6, dependence_kind="score",
                  correlation=0.9, created_at=T)
    ok("a score edge does not create a loss linkage",
       sizing.loss_linked(con, a) == [], sizing.loss_linked(con, a))
    ok("...so nothing is counted against the concentration cap",
       sizing.cluster_exposure_used_usd(con, a) == 0.0)
    evaluate.link(con, a, b, conditional_prob=0.6, dependence_kind="loss",
                  correlation=0.8, created_at=T)
    ok("a loss edge does", sizing.loss_linked(con, a) == [b])
    ok("an unlinked forecast has no group", sizing.loss_linked(con, b) == [])

    fid_b = con.execute("SELECT id FROM forecasts WHERE forecast_hash=?",
                        (b,)).fetchone()[0]
    cid = con.execute("SELECT id FROM contracts").fetchone()[0]
    con.execute(
        """INSERT INTO trade_decisions
           (forecast_id, contract_id, decided_at, intended_size_usd, permitted,
            mode, max_notional_usd, cluster_exposure_cap_usd, eligibility_status)
           VALUES (?,?,?,250.0,1,'paper',1000.0,1000.0,'close_only')""",
        (fid_b, cid, T))
    con.commit()
    ok("a permitted paper decision counts toward concentration",
       sizing.cluster_exposure_used_usd(con, a) == 250.0,
       sizing.cluster_exposure_used_usd(con, a))
    con.execute(
        """INSERT INTO trade_decisions
           (forecast_id, contract_id, decided_at, intended_size_usd, permitted,
            abstain_reason, mode, max_notional_usd, cluster_exposure_cap_usd,
            eligibility_status)
           VALUES (?,?,?,900.0,0,'EV below floor','paper',1000.0,1000.0,
                   'close_only')""",
        (fid_b, cid, T))
    con.commit()
    ok("an abstention does not", sizing.cluster_exposure_used_usd(con, a) == 250.0)

    print("\n[6] n_eff appears nowhere in this module\n")
    src = open(os.path.join(ROOT, "spine", "sizing.py"), encoding="utf-8").read()
    body = src.split('"""', 2)[2]      # past the module docstring
    for token in ("n_eff", "effective_sample", "bss", "brier"):
        ok(f"no {token!r} in the sizing logic", token not in body.lower())
    ok("the module docstring says why", "conflates statistical evidence" in src)

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

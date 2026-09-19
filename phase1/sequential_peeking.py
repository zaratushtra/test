#!/usr/bin/env python3
"""
What unbudgeted peeking costs, and whether alpha spending fixes it.

v2 §12 asserts that repeatedly checking a 95% interval and stopping at the first
favourable result is p-hacking. Assertion is cheap. This measures it, the same
way `serial_dependence.py` measured the bootstrap failure, and then checks that
`spine/sequential.py` actually restores the nominal rate.

The scenario is not adversarial. Forecasts accumulate week by week, the gate is
cheap to run, and someone checks whether the record is good enough yet. Nobody
intends to cheat. The error rate rises anyway.

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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spine import sequential  # noqa: E402


def run_experiment(rng: random.Random, n_looks: int, per_look: int,
                   true_effect: float, sd: float) -> list[tuple[float, float]]:
    """
    Accumulate data and report (point estimate, standard error) at each look.

    One observation per regime per look, so the standard error is the honest
    between-regime one rather than a within-regime figure that would understate.
    """
    data: list[float] = []
    out = []
    for _ in range(n_looks):
        data.extend(rng.gauss(true_effect, sd) for _ in range(per_look))
        n = len(data)
        se = statistics.pstdev(data) / math.sqrt(n) if n > 1 else float("inf")
        out.append((statistics.fmean(data), se))
    return out


def fixed_gate_rate(trials: int, n_looks: int, per_look: int, true_effect: float,
                    sd: float, seed: int = 3) -> tuple[float, float]:
    """
    Fixed 95% lower bound, checked at EVERY look, stopping at the first pass.

    Returns (rate at the final look only, rate when peeking at every look).
    """
    rng = random.Random(seed)
    final_only = peeked = 0
    for _ in range(trials):
        looks = run_experiment(rng, n_looks, per_look, true_effect, sd)
        if any(p - 1.96 * se > 0 for p, se in looks):
            peeked += 1
        p, se = looks[-1]
        if p - 1.96 * se > 0:
            final_only += 1
    return final_only / trials, peeked / trials


def spending_gate_rate(trials: int, n_looks: int, per_look: int,
                       true_effect: float, sd: float, spending: str,
                       alpha: float = 0.05, seed: int = 3) -> float:
    """Same peeking behaviour, but thresholds come from the spending schedule."""
    sched = sequential.schedule(n_looks, alpha=alpha, spending=spending)
    rng = random.Random(seed)
    fired = 0
    for _ in range(trials):
        looks = run_experiment(rng, n_looks, per_look, true_effect, sd)
        if any(sequential.gate_fires(p, se, lk) for (p, se), lk in zip(looks, sched)):
            fired += 1
    return fired / trials


def main() -> int:
    ap = argparse.ArgumentParser(description="Sequential peeking analysis")
    ap.add_argument("--trials", type=int, default=3000)
    ap.add_argument("--per-look", type=int, default=12)
    ap.add_argument("--sd", type=float, default=1.0)
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    print("=" * 76)
    print("SPINE — THE COST OF PEEKING, AND WHETHER ALPHA SPENDING FIXES IT")
    print("=" * 76)
    print(f"{args.trials} trials, {args.per_look} new observations per look, sd={args.sd}\n")

    print("[1] Fixed 95% bound, true effect = 0 (so every pass is a false positive)\n")
    print(f"{'looks':>7}{'final look only':>18}{'checked every look':>21}{'inflation':>12}")
    print("-" * 76)
    fixed_rows = []
    for n_looks in (1, 2, 5, 10, 20, 52):
        final, peek = fixed_gate_rate(args.trials, n_looks, args.per_look, 0.0, args.sd)
        infl = peek / final if final > 0 else float("inf")
        infl_s = "-" if n_looks == 1 else f"{infl:.1f}x"
        print(f"{n_looks:>7}{final:>17.1%}{peek:>20.1%}{infl_s:>12}")
        fixed_rows.append({"n_looks": n_looks, "final_only": final, "peeked": peek})

    print(f"\n  A weekly check over one year ({52} looks) turns a nominal 2.5% gate into")
    print(f"  {fixed_rows[-1]['peeked']:.0%}. Nobody had to act in bad faith.")

    print(f"\n[2] The same peeking under an alpha-spending schedule\n")
    print(f"{'looks':>7}{'spending':>18}{'effect 0 (FPR)':>17}{'effect 0.5 (power)':>21}")
    print("-" * 76)
    spend_rows = []
    for n_looks in (5, 10, 20, 52):
        for spending in ("pocock", "obrien_fleming"):
            fpr = spending_gate_rate(args.trials, n_looks, args.per_look, 0.0,
                                     args.sd, spending)
            pwr = spending_gate_rate(args.trials, n_looks, args.per_look, 0.5,
                                     args.sd, spending)
            flag = "" if fpr <= 0.075 else "  <- still inflated"
            print(f"{n_looks:>7}{spending:>18}{fpr:>16.1%}{pwr:>20.1%}{flag}")
            spend_rows.append({"n_looks": n_looks, "spending": spending,
                               "fpr": fpr, "power": pwr})

    print(f"\n[3] What a schedule looks like (20 looks, O'Brien-Fleming)\n")
    print(sequential.describe(sequential.schedule(20, 0.05, "obrien_fleming")[:6]))
    print("      ...")
    last = sequential.schedule(20, 0.05, "obrien_fleming")[-1]
    print(f"{last.index:>5}{last.information_fraction:>8.2f}"
          f"{last.alpha_this_look:>10.5f}{last.alpha_spent_cumulative:>10.5f}"
          f"{last.z_threshold:>8.3f}")

    print(f"\n{'='*76}\nFINDING\n{'='*76}")
    print(f"""
Checking a fixed 95% bound weekly for a year inflates the false positive rate
from 2.5% to {fixed_rows[-1]['peeked']:.0%} — an order of magnitude, with no bad faith required. The
gate is cheap, the data accumulates, and someone looks.

Alpha spending restores the nominal rate while keeping usable power. O'Brien-
Fleming is the right default here: it is severe early and approaches nominal at
the end, so an early stop is rare and meaningful, and almost all power is
preserved for the full schedule. Pocock trades final-look power for a genuine
chance of stopping early, which this project does not need.

The number of looks must be declared BEFORE the record starts accumulating.
Choosing it afterwards, once the shape of the data is known, reintroduces
exactly the freedom the schedule removes.
""".strip())

    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "sequential_peeking.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"trials": args.trials, "per_look": args.per_look,
                   "fixed": fixed_rows, "spending": spend_rows}, fh, indent=2)
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

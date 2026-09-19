#!/usr/bin/env python3
"""
Phase 0(c) — throughput pilot harness.

**Section numbers here were v1's and are corrected to v2's.** The three this
file originally cited now point at conditional probability, gate zero and
verification instead — a reminder that a reference by number survives a rewrite
looking exactly as authoritative as it did before. The check in
tests/test_docs.py exists so the next one is caught rather than read.

The substance stands. §3.1 requires `n_eff ≳ 31.4 / BSS` independent forecasts —
**314 at BSS = 0.10**, not the 250 this file used to claim, which predates the
coefficient correction. If a forecast costs four analyst-hours that is well over
a thousand hours before the resolution horizon is even reached, and nobody has
measured the per-forecast cost.

This times each stage for manually produced forecasts and projects the implied
Register A timeline. Run it for five forecasts; the answer decides whether the
roadmap in §13 is real.

    python throughput.py time --market-id 0x123 --question "..."   # interactive
    python throughput.py report

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from datetime import datetime, timezone

STAGES = [
    ("stage0_admission", "Stage 0 — write event, resolution criterion, deadline; run the checklist"),
    ("stage1_base_rate", "Stage 1 — freeze the reference class (spine/refclass.freeze)"),
    ("stage2_pressure", "Stage 2 — assemble indicators (skip for T1)"),
    ("stage3_incentive", "Stage 3 — actors, payoff orderings, best-response check"),
    ("stage4_dag", "Stage 4 — locate in the DAG, estimate correlations with siblings"),
    # The firewall this stage used to check was removed in v2 §5.1: it capped
    # the probability of an event rather than confidence in the estimate.
    ("stage5_record", "Stage 5 — final probability, interval, record"),
]

LOG = "out/throughput_log.jsonl"


def cmd_time(args: argparse.Namespace) -> int:
    print(f"\nTiming a forecast. Press Enter to start each stage, Enter again to stop.")
    print(f"Market: {args.question or args.market_id}\n")
    record = {
        "market_id": args.market_id,
        "question": args.question,
        "tier": args.tier,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "durations_sec": {},
        "notes": {},
    }
    for key, desc in STAGES:
        if args.tier == "T1" and key == "stage2_pressure":
            print(f"  [skip] {desc}")
            record["durations_sec"][key] = 0.0
            continue
        input(f"\n  START  {desc}\n         press Enter to begin... ")
        t0 = time.monotonic()
        input("         working... press Enter when done. ")
        elapsed = time.monotonic() - t0
        record["durations_sec"][key] = round(elapsed, 1)
        note = input("         note (optional): ").strip()
        if note:
            record["notes"][key] = note
        print(f"         -> {elapsed/60:.1f} min")

    total = sum(record["durations_sec"].values())
    record["total_sec"] = round(total, 1)
    record["finished_at"] = datetime.now(timezone.utc).isoformat()

    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    print(f"\n  TOTAL: {total/60:.1f} min  ->  appended to {LOG}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    if not os.path.exists(LOG):
        print(f"No log at {LOG}. Run `time` on at least 3 forecasts first.",
              file=sys.stderr)
        return 2
    records = [json.loads(line) for line in open(LOG, encoding="utf-8") if line.strip()]
    if not records:
        print("Log is empty.", file=sys.stderr)
        return 2

    print(f"\n{'='*68}\nSPINE PHASE 0(c) — THROUGHPUT PILOT\n{'='*68}")
    print(f"forecasts timed: {len(records)}\n")
    print(f"{'stage':<26}{'median min':>12}{'max min':>10}{'share':>9}")
    print("-" * 68)

    totals = [r["total_sec"] for r in records]
    median_total = statistics.median(totals)
    for key, _ in STAGES:
        vals = [r["durations_sec"].get(key, 0.0) for r in records]
        med = statistics.median(vals)
        share = med / median_total * 100 if median_total else 0
        print(f"{key:<26}{med/60:>12.1f}{max(vals)/60:>10.1f}{share:>8.0f}%")
    print("-" * 68)
    print(f"{'TOTAL':<26}{median_total/60:>12.1f}{max(totals)/60:>10.1f}")

    # Projection against the §2 requirement.
    hours_each = median_total / 3600
    per_week = args.analyst_hours / hours_each if hours_each else 0
    # Independent forecasts only — correlated ones do not accumulate n_eff.
    n_eff_week = per_week / (1 + (per_week - 1) * args.r_bar) if per_week > 1 else per_week
    need = 24.7 / args.target_bss
    weeks = need / n_eff_week if n_eff_week > 0 else float("inf")

    print(f"\n{'-'*68}\nPROJECTION\n{'-'*68}")
    print(f"analyst hours available / week : {args.analyst_hours:.0f}")
    print(f"median hours / forecast        : {hours_each:.2f}")
    print(f"forecasts producible / week    : {per_week:.1f}")
    print(f"assumed r_bar                  : {args.r_bar:.2f}")
    print(f"n_eff / week                   : {n_eff_week:.2f}")
    print(f"n_eff needed @ BSS={args.target_bss:.2f}     : {need:.0f}")
    print(f"projected Register A duration  : {weeks:.0f} weeks "
          f"(~{weeks/4.33:.1f} months)")

    verdict = "PASS" if weeks <= 78 else "FAIL"
    print(f"\nGATE: {verdict}  (threshold: Register A reachable within 18 months)")
    if verdict == "FAIL":
        print("\n  Throughput is the binding constraint. Options: automate part of\n"
              "  Stage 3, add analysts, raise the claimed effect size, or stop.")

    stage3 = statistics.median([r["durations_sec"].get("stage3_incentive", 0)
                                for r in records])
    if median_total and stage3 / median_total > 0.4:
        print(f"\n  Stage 3 is {stage3/median_total*100:.0f}% of the cost.\n"
              "  It is the highest-leverage automation target in the project.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE Phase 0 throughput pilot")
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("time", help="time one forecast interactively")
    t.add_argument("--market-id", default="")
    t.add_argument("--question", default="")
    t.add_argument("--tier", choices=["T1", "T2", "T3"], default="T1")
    t.set_defaults(func=cmd_time)

    r = sub.add_parser("report", help="summarise the log and project the timeline")
    r.add_argument("--analyst-hours", type=float, default=20.0,
                   help="analyst hours available per week")
    r.add_argument("--r-bar", type=float, default=0.10,
                   help="assumed mean correlation between forecasts")
    r.add_argument("--target-bss", type=float, default=0.10)
    r.set_defaults(func=cmd_report)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

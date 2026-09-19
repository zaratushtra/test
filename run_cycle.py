#!/usr/bin/env python3
"""
One operating cycle: screen the venue, register contracts, record books.

This is the entry point to run from a host that has network. Everything it does
is read-only against the venue — there is no authentication path in this
codebase — and everything it records is point-in-time.

    python3 run_cycle.py --jurisdiction GB --db spine.db
    python3 run_cycle.py --check                      # reachability only
    python3 run_cycle.py --from-dir saved/ --db spine.db   # offline replay

Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "phase0"))

import screen_markets  # noqa: E402
from spine import collect, evaluate, ledger, registry, shadow, venue  # noqa: E402


def now() -> datetime:
    return datetime.now(timezone.utc)


def banner(text: str) -> None:
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE operating cycle")
    ap.add_argument("--db", default="spine.db", help="database path")
    ap.add_argument("--jurisdiction", default="unknown",
                    help="ISO-3166 code; gates trading eligibility")
    ap.add_argument("--limit", type=int, default=3000, help="markets to fetch")
    ap.add_argument("--zones", default="T1,T2",
                    help="comma-separated zones to register")
    ap.add_argument("--check", action="store_true",
                    help="probe venue reachability and exit")
    ap.add_argument("--from-dir", help="replay saved JSON instead of fetching")
    ap.add_argument("--save-dir", help="write raw responses here for later replay")
    ap.add_argument("--no-books", action="store_true",
                    help="register contracts but do not record books")
    ap.add_argument("--sweep", action="store_true",
                    help="also run every declared collection query")
    ap.add_argument("--record-look", action="store_true",
                    help="spend one of the budgeted evaluation looks (irreversible)")
    args = ap.parse_args()

    # ---------------------------------------------------------- reachability
    if not args.from_dir:
        banner("VENUE REACHABILITY")
        reach = venue.check_reachability()
        print(f"  {reach.summary()}")
        if args.check:
            return 0 if reach.ok else 2
        if not reach.ok:
            print("\n  Cannot proceed: the venue is not reachable from this host.\n"
                  "  Both endpoints are public and need no key, so this is a\n"
                  "  network question, not an access one. Run from a machine with\n"
                  "  outbound HTTPS, or replay saved JSON with --from-dir.",
                  file=sys.stderr)
            return 2
    elif args.check:
        print("--check probes the network; it has nothing to do with --from-dir",
              file=sys.stderr)
        return 2

    # ---------------------------------------------------------- markets
    banner("MARKETS")
    if args.from_dir:
        path = os.path.join(args.from_dir, "markets.json")
        raw = venue.load_saved(path)
        print(f"  replayed {len(raw)} markets from {path}")
    else:
        raw = venue.fetch_markets(
            args.limit, on_page=lambda n: print(f"  fetched {n}", end="\r",
                                                file=sys.stderr))
        print(f"  fetched {len(raw)} markets")
        if args.save_dir:
            os.makedirs(args.save_dir, exist_ok=True)
            with open(os.path.join(args.save_dir, "markets.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(raw, fh)
            print(f"  saved raw response to {args.save_dir}/markets.json")

    if not raw:
        print("  no markets returned; nothing to do", file=sys.stderr)
        return 2

    markets = screen_markets.normalise(raw, now())
    wanted = {z.strip() for z in args.zones.split(",") if z.strip()}
    selected = [m for m in markets if m.zone in wanted]
    by_zone: dict[str, int] = {}
    for m in markets:
        by_zone[m.zone] = by_zone.get(m.zone, 0) + 1
    print(f"  zones: " + "  ".join(f"{z}={n}" for z, n in sorted(by_zone.items())))
    print(f"  selected {len(selected)} in {sorted(wanted)}")

    have_rules = sum(1 for m in selected if m.rules_text)
    if selected and have_rules < len(selected) * 0.5:
        print(f"\n  WARNING: only {have_rules}/{len(selected)} selected markets "
              "carry resolution text.\n  FIELD_CANDIDATES['rules_text'] is "
              "probably stale — run\n  phase0/screen_markets.py --dump-schema and "
              "fix the key list.", file=sys.stderr)

    # ---------------------------------------------------------- registry
    banner("CONTRACT REGISTRY")
    try:
        con = ledger.connect(args.db)
    except ledger.LedgerError as e:
        print(f"\n  {e}", file=sys.stderr)
        return 3
    rep = registry.ingest_markets(con, [asdict(m) for m in selected],
                                  jurisdiction=args.jurisdiction)
    print(f"  {rep.summary()}")
    if rep.skipped:
        reasons: dict[str, int] = {}
        for _, why in rep.skipped:
            key = why.split(":")[0]
            reasons[key] = reasons.get(key, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>5}  {why}")

    total = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    elig = con.execute(
        "SELECT eligibility_status, COUNT(*) FROM contracts "
        "GROUP BY eligibility_status").fetchall()
    print(f"  registry now holds {total} contracts: "
          + ", ".join(f"{n} {s}" for s, n in elig))

    if args.jurisdiction.upper() in registry.CLOSE_ONLY:
        print(f"\n  {args.jurisdiction.upper()} is close-only: no new positions can "
              "be opened.\n  Everything below is research and shadow execution. "
              "That is the\n  expected posture, not a failure.")

    # ---------------------------------------------------------- books
    if args.no_books:
        con.close()
        return 0

    banner("ORDER BOOKS")
    contracts = [dict(zip([d[0] for d in cur.description], r))
                 for cur in [con.execute(
                     "SELECT id, market_id, outcome_token_id FROM contracts "
                     "ORDER BY id")]
                 for r in cur.fetchall()]
    if args.from_dir:
        books = venue.load_saved(os.path.join(args.from_dir, "books.json"))
        result = venue.snapshot_books(con, contracts, source="replay",
                                      fetcher=lambda t: books[t])
    else:
        result = venue.snapshot_books(con, contracts)
        if args.save_dir:
            print("  (raw books are not saved; re-running re-reads them)")

    print(f"  recorded {result['recorded']} book snapshots, "
          f"{len(result['skipped'])} skipped")
    for mid, why in result["skipped"][:8]:
        print(f"    {mid}: {why}")
    if len(result["skipped"]) > 8:
        print(f"    ... and {len(result['skipped']) - 8} more")

    if result["snapshot_ids"]:
        spreads = []
        for sid in result["snapshot_ids"]:
            b = shadow.load_book(con, sid)
            if b.spread_bp is not None:
                spreads.append(b.spread_bp)
        if spreads:
            spreads.sort()
            print(f"  spread: median {spreads[len(spreads)//2]}bp, "
                  f"min {spreads[0]}bp, max {spreads[-1]}bp "
                  f"(over {len(spreads)} two-sided books)")
            one_sided = len(result["snapshot_ids"]) - len(spreads)
            if one_sided:
                print(f"  {one_sided} book(s) one-sided: no midpoint, "
                      "no markout reference")

    # ---------------------------------------------------------- signals
    if args.sweep:
        banner("SIGNAL COLLECTION")
        queries = collect.active_queries(con)
        if not queries:
            print("  No collection queries declared.\n"
                  "  Collection is anchored to registered propositions, so a query\n"
                  "  needs a proposition first (spine/registry.ensure_proposition)\n"
                  "  and then spine/collect.declare_query. See docs/RUNNING.md.")
        else:
            print(f"  {len(queries)} active quer{'y' if len(queries)==1 else 'ies'}")
            results = collect.sweep(con, fetcher=None if not args.from_dir
                                    else (lambda u: b""))
            new_items = sum(r.ingested for r in results)
            dupes = sum(r.duplicate for r in results)
            failed = [r for r in results if r.outcome != "ok"]
            print(f"  {new_items} new items, {dupes} already held, "
                  f"{len(failed)} quer{'y' if len(failed)==1 else 'ies'} failed")
            for r in failed[:6]:
                print(f"    {r.summary()}")

    # ---------------------------------------------------------- evaluation
    banner("EVALUATION")
    run = evaluate.score_all(con)
    print(f"  {run.summary()}")
    sl = evaluate.slices(con)
    if not sl:
        print("  No forecasts registered yet; nothing to evaluate.")
    else:
        print(f"  {len(sl)} slice(s) in the record — these must not be averaged "
              "together:")
        for row in sl[:6]:
            print(f"    {row['forecast_kind']:<20}{row['model_version']:<12}"
                  f"{row['event_family']:<24}n={row['n']:<6}regimes={row['regimes']}")
        # A dry run by default: inspecting the record must not spend alpha.
        # --record-look is the deliberate act of taking one of the budgeted
        # looks, and it is irreversible.
        ev = evaluate.evaluate(con, record_look=args.record_look)
        print("  " + ev.summary().replace("\n", "\n  "))
        if ev.blocked_by and "no evaluation plan" in ev.blocked_by:
            print("\n  Declare one before the record grows (docs/RUNNING.md):\n"
                  "    evaluate.declare_plan(con, n_looks=..., declared_by='you')\n"
                  "  The number of looks fixed after seeing the data is not a\n"
                  "  budget, and phase1/sequential_peeking.py measured what that\n"
                  "  costs: a weekly fixed-bound check turns a 3% gate into 19%.")
        div = evaluate.settlement_divergence(con)
        if div:
            print(f"  {len(div)} contract(s) settled against the research outcome — "
                  "the instrument did not measure the question:")
            for d in div[:5]:
                print(f"    {d['market_id']}: paid {d['payout_per_share']} on "
                      f"{d['outcome']} (binding: {d['match_quality']})")

    banner("NEXT")
    print("  Re-run this on a schedule to build the book history shadow execution\n"
          "  needs. Passive fill modelling additionally requires the CLOB trades\n"
          "  channel: depth changes cannot distinguish a fill from a cancellation\n"
          "  (docs/SHADOW-EXECUTION-REPORT.md §2).")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

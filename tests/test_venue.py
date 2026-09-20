#!/usr/bin/env python3
"""
Venue access validation: book parsing, the units conversion, and failure modes.

Nothing here touches the network. The parsing is what breaks quietly — a shares
count read as dollars, or a failed read stored as a thin book — and those are
testable without leaving the machine.

Run: python3 tests/test_venue.py
Stdlib only.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger, registry, shadow, venue  # noqa: E402
from spine.venue import VenueError  # noqa: E402

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


def message(fn) -> str:
    """The exception text, which is part of the interface when a refusal explains itself."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""



BOOK = {
    "bids": [{"price": "0.61", "size": "1000"}, {"price": "0.60", "size": "2500"}],
    "asks": [{"price": "0.63", "size": "800"}, {"price": "0.65", "size": "4000"}],
}


def main() -> int:
    print("=" * 76)
    print("SPINE VENUE ACCESS VALIDATION")
    print("=" * 76)

    print("\n[1] Sizes arrive in shares and must leave in dollars\n")
    bids, asks = venue.parse_book(BOOK)
    ok("prices become basis points", bids[0][0] == 6100 and asks[0][0] == 6300)
    ok("1000 shares at 0.61 is $610 of notional, not $1000",
       abs(bids[0][1] - 610.0) < 1e-9, bids[0][1])
    ok("...which is 39% less depth than the raw share count implies",
       abs(1 - bids[0][1] / 1000.0 - 0.39) < 0.005)
    ok("800 shares at 0.63 is $504", abs(asks[0][1] - 504.0) < 1e-9)
    ok("the array form parses identically",
       venue.parse_book({"bids": [["0.61", "1000"]], "asks": [["0.63", "800"]]})[0]
       == [(6100, 610.0)])

    print("\n[2] A failed read is not a thin book\n")
    ok("a missing side is refused, not read as empty",
       raises(lambda: venue.parse_book({"asks": []}), VenueError))
    ok("...and the message says why that distinction matters",
       "not an empty side" in message(lambda: venue.parse_book({"asks": []})))
    ok("an explicitly empty side parses as empty",
       venue.parse_book({"bids": [], "asks": []}) == ([], []))
    ok("a non-object payload is refused",
       raises(lambda: venue.parse_book([1, 2, 3]), VenueError))
    ok("an unparseable level is refused rather than skipped",
       raises(lambda: venue.parse_book({"bids": [{"price": "x", "size": "1"}],
                                        "asks": []}), VenueError))
    ok("a zero-size level is dropped",
       venue.parse_book({"bids": [{"price": "0.5", "size": "0"}],
                         "asks": []})[0] == [])
    ok("a price at 0 or 1 is dropped: that is a settled market, not a tradeable one",
       venue.parse_book({"bids": [{"price": "1.0", "size": "10"},
                                  {"price": "0", "size": "10"}],
                         "asks": []})[0] == [])
    print("\n[2b] Rounding must not land on an endpoint\n")
    edge = venue.parse_book({"bids": [{"price": "0.00005", "size": "1000"}],
                             "asks": [{"price": "0.99995", "size": "1000"}]})
    ok("a price rounding to 0 is clamped to 1bp", edge[0][0][0] == 1, edge[0])
    ok("a price rounding to 1.0 is clamped to 9999bp", edge[1][0][0] == 9999,
       edge[1])
    ok("...because every price invariant here is the OPEN interval",
       0 < edge[0][0][0] < 10000 and 0 < edge[1][0][0] < 10000)
    # The concrete consequence: without the clamp, the decision table refuses to
    # store an acquisition price derived from such a book.
    con0 = ledger.connect(":memory:", create=True)
    registry.ingest_markets(con0, [{"id": "m", "question": "q",
                                    "end_date": "2026-12-01T00:00:00Z",
                                    "rules_text": "r", "outcome_token_id": "t"}],
                            jurisdiction="GB")
    sid = shadow.record_book(con0, 1, edge[0], edge[1],
                             captured_at="2026-09-19T00:00:00.000Z",
                             source="fixture")
    bk = shadow.load_book(con0, sid)
    ok("a near-certain book still yields a storable acquisition price",
       0 < shadow.aggressive_fill(bk, "YES", 100.0, 9999).avg_price_bp < 10000)

    ok("a book request with no token is refused",
       raises(lambda: venue.fetch_book(""), VenueError))

    print("\n[3] Collection survives a bad token without shrinking silently\n")
    con = ledger.connect(":memory:", create=True)
    rows = [{"id": f"m{i}", "question": f"Q{i}", "end_date": "2026-12-01T00:00:00Z",
             "rules_text": "Resolves on the named source.",
             "outcome_token_id": f"tok{i}"} for i in range(4)]
    registry.ingest_markets(con, rows, jurisdiction="GB")
    contracts = [dict(zip([d[0] for d in c.description], r))
                 for c in [con.execute(
                     "SELECT id, market_id, outcome_token_id FROM contracts")]
                 for r in c.fetchall()]

    responses = {
        "tok0": BOOK,
        "tok1": {"bids": [], "asks": []},                       # genuinely empty
        "tok2": {"asks": [{"price": "0.5", "size": "10"}]},     # failed read
        "tok3": {"bids": [{"price": "0.70", "size": "100"}],    # crossed
                 "asks": [{"price": "0.60", "size": "100"}]},
    }
    res = venue.snapshot_books(con, contracts, source="fixture",
                               fetcher=lambda t: responses[t])
    ok("the good book is recorded", res["recorded"] == 1, res["recorded"])
    ok("three failures are counted, not dropped", len(res["skipped"]) == 3)
    whys = {m: w for m, w in res["skipped"]}
    ok("an empty book says it is empty", "no resting liquidity" in whys["m1"])
    ok("a missing side says it was a failed read", "not an empty side" in whys["m2"])
    ok("a crossed book is refused at the storage layer", "crossed" in whys["m3"])
    ok("one bad token does not abort the run", res["recorded"] + len(res["skipped"]) == 4)

    # A fetcher can raise anything at all -- a replay source missing a key, a
    # socket error from deep in urllib, a decoder failing on a truncated body.
    # The narrow except used to let those through and abort the whole run, which
    # an offline replay with a missing token found by raising KeyError.
    def hostile(token):
        raise KeyError(token)

    wild = venue.snapshot_books(con, contracts, source="fixture",
                                fetcher=hostile)
    ok("a fetcher raising something unexpected does not abort the run",
       wild["recorded"] == 0 and len(wild["skipped"]) == 4, wild)
    ok("...and the exception type is recorded, not swallowed",
       all("KeyError" in why for _, why in wild["skipped"]), wild["skipped"])

    b = shadow.load_book(con, res["snapshot_ids"][0])
    ok("the recorded book round-trips through storage",
       b.best_bid_bp == 6100 and b.best_ask_bp == 6300 and b.spread_bp == 200)
    ok("...with notional depth, so a walk prices in dollars",
       abs(b.asks[0].size_usd - 504.0) < 1e-9)

    print("\n[4] Reachability is reported, never guessed\n")
    def dead(_url, timeout=10):
        raise venue.Unreachable("cannot reach; no route")
    saved = venue._get
    try:
        venue._get = dead
        r = venue.check_reachability()
        ok("an unreachable venue reports unreachable, both endpoints",
           not r.ok and "unreachable" in r.gamma and "unreachable" in r.clob,
           r.summary())
        venue._get = lambda u, timeout=10: (_ for _ in ()).throw(
            VenueError("HTTP 500"))
        r2 = venue.check_reachability()
        ok("a reachable-but-broken venue is a different answer",
           "reachable but errored" in r2.gamma, r2.summary())
        ok("...which matters because the two need different responses",
           r.gamma != r2.gamma)
    finally:
        venue._get = saved

    print("\n[5] There is no authentication path in this module\n")
    src = open(os.path.join(ROOT, "spine", "venue.py"), encoding="utf-8").read()
    for word in ("Authorization", "api_key", "apiKey", "private_key", "sign(",
                 "secret"):
        ok(f"no {word!r} anywhere in the venue client", word not in src)

    print("\n[6] The saved-response path, for a host with no network\n")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "markets.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump([{"id": "m1"}], fh)
        ok("saved responses load", venue.load_saved(p) == [{"id": "m1"}])

    print("\n[7] The operating cycle runs offline, end to end\n")
    import subprocess
    with tempfile.TemporaryDirectory() as d:
        saved = os.path.join(d, "saved")
        os.makedirs(saved)
        with open(os.path.join(saved, "markets.json"), "w", encoding="utf-8") as fh:
            json.dump([{"id": "c1", "question": "Will it happen?",
                        "endDate": "2026-09-28T00:00:00Z", "liquidityNum": 40000,
                        "description": "Resolves on the named source.",
                        "clobTokenIds": json.dumps(["ct1", "ct2"]),
                        "events": [{"id": "e1", "tags": [{"label": "Policy"}]}]}], fh)
        with open(os.path.join(saved, "books.json"), "w", encoding="utf-8") as fh:
            json.dump({"ct1": BOOK}, fh)
        db = os.path.join(d, "cycle.db")
        cmd = [sys.executable, os.path.join(ROOT, "run_cycle.py"),
               "--from-dir", saved, "--db", db, "--jurisdiction", "GB"]
        r1 = subprocess.run(cmd, capture_output=True, text=True)
        ok("the cycle completes", r1.returncode == 0, r1.stderr[-300:])
        ok("it registers the contract", "1 new" in r1.stdout, r1.stdout[-300:])
        ok("it records the book", "recorded 1 book" in r1.stdout)
        ok("it states the close-only posture rather than burying it",
           "close-only" in r1.stdout)
        r2 = subprocess.run(cmd, capture_output=True, text=True)
        ok("re-running registers nothing new", "0 new" in r2.stdout)
        ok("...but records a fresh book, because a later capture is new data",
           "recorded 1 book" in r2.stdout)
        import sqlite3 as _sq
        c = _sq.connect(db)
        ok("the database holds one contract and two snapshots",
           c.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 1 and
           c.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 2)
        c.close()

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

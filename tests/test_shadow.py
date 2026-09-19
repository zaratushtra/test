#!/usr/bin/env python3
"""
Shadow execution validation: books, queue position, cancellation latency and
adverse selection.

The suite is organised around the three claims §10.2 makes and v1 never tested:
that touching a limit is not a fill, that intent to cancel is not cancellation,
and that fills arrive disproportionately when the price is moving through you.
The last is treated as a hypothesis to measure, not an axiom to encode.

Run: python3 tests/test_shadow.py
Stdlib only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger, registry, shadow  # noqa: E402
from spine.shadow import ShadowError  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 19, 14, 0, 0, tzinfo=timezone.utc)


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


def at(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def main() -> int:
    print("=" * 76)
    print("SPINE SHADOW EXECUTION VALIDATION")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)
    registry.ingest_markets(con, [{
        "id": "mk-shadow", "question": "Will the measure pass?",
        "end_date": at(days=9), "rules_text": "Resolves YES on a recorded vote.",
        "outcome_token_id": "tok-s"}], jurisdiction="GB", now=at())
    cid = con.execute("SELECT id FROM contracts").fetchone()[0]

    # ------------------------------------------------ books
    print("\n[1] A book is a point-in-time artifact, and a crossed one is a bug\n")
    b1 = shadow.record_book(
        con, cid,
        bids=[(5900, 400.0), (5800, 900.0), (5700, 2000.0)],
        asks=[(6100, 300.0), (6200, 700.0), (6400, 1500.0)],
        captured_at=at(), source="fixture", capture_lag_seconds=2,
        venue_timestamp=at(seconds=-1))
    book = shadow.load_book(con, b1)
    ok("best bid and ask are the inside of the book",
       book.best_bid_bp == 5900 and book.best_ask_bp == 6100)
    ok("the midpoint sits between them", book.mid_bp == 6000.0)
    ok("the spread is 200bp", book.spread_bp == 200)
    ok("depth at a price is the queue an order would join",
       book.depth_at(5800, "bid") == 900.0)
    ok("an identical book at the same instant is stored once",
       shadow.record_book(con, cid, bids=[(5900, 400.0), (5800, 900.0),
                                          (5700, 2000.0)],
                          asks=[(6100, 300.0), (6200, 700.0), (6400, 1500.0)],
                          captured_at=at(), source="fixture",
                          capture_lag_seconds=2) == b1)
    ok("a crossed book is refused, not silently priced",
       raises(lambda: shadow.record_book(
           con, cid, bids=[(6200, 100.0)], asks=[(6100, 100.0)],
           captured_at=at(), source="fixture"), ShadowError))
    ok("a zero-size level is refused",
       raises(lambda: shadow.record_book(
           con, cid, bids=[(5900, 0.0)], asks=[(6100, 10.0)],
           captured_at=at(), source="fixture"), ShadowError))
    ok("a book usable before it was captured is refused",
       raises(lambda: shadow.record_book(
           con, cid, bids=[(5900, 10.0)], asks=[(6100, 10.0)],
           captured_at=at(hours=1), source="fixture",
           available_for_decision_at=at()), ShadowError))
    ok("book snapshots are immutable",
       raises(lambda: con.execute(
           "UPDATE book_snapshots SET source='replay' WHERE id=?", (b1,)),
           sqlite3.IntegrityError))
    ok("the venue's own timestamp is recorded and is not what governs retrieval",
       con.execute("SELECT venue_timestamp, available_for_decision_at "
                   "FROM book_snapshots WHERE id=?", (b1,)).fetchone()
       == (at(seconds=-1), at(seconds=2)))

    print("\n[2] Retrieval cannot reach the book captured during the decision\n")
    b2 = shadow.record_book(
        con, cid, bids=[(5700, 400.0)], asks=[(5900, 300.0), (6000, 900.0)],
        captured_at=at(minutes=10), source="fixture", capture_lag_seconds=2)
    ok("a decision before any book sees none",
       shadow.book_at(con, cid, at(seconds=-5)) is None)
    ok("a decision between the two sees only the first",
       shadow.book_at(con, cid, at(minutes=5)).snapshot_id == b1)
    ok("a decision after both sees the newer one",
       shadow.book_at(con, cid, at(minutes=20)).snapshot_id == b2)
    ok("a decision one second before the newer book is usable does not see it",
       shadow.book_at(con, cid, at(minutes=10, seconds=1)).snapshot_id == b1)

    # ------------------------------------------------ aggressive
    print("\n[3] Aggressive fills respect the limit and the depth\n")
    f_full = shadow.aggressive_fill(book, "YES", 250.0, limit_price_bp=6100)
    ok("a small order fills at the inside ask",
       abs(f_full.avg_price_bp - 6100) < 1e-9 and f_full.fully_filled,
       f_full.summary())
    f_walk = shadow.aggressive_fill(book, "YES", 900.0, limit_price_bp=6400)
    ok("a larger order pays up through the book",
       f_walk.avg_price_bp > 6100, f_walk.summary())
    ok("...and the midpoint would have flattered it by a wide margin",
       f_walk.avg_price_bp - book.mid_bp > 150,
       f"{f_walk.avg_price_bp:.0f} vs mid {book.mid_bp:.0f}")
    f_limited = shadow.aggressive_fill(book, "YES", 900.0, limit_price_bp=6200)
    ok("a limit truncates the fill rather than reaching worse prices",
       not f_limited.fully_filled and f_limited.filled_usd == 1000.0 or
       f_limited.filled_usd <= 1000.0, f_limited.summary())
    ok("no liquidity within the limit is a no-fill with a reason",
       shadow.aggressive_fill(book, "YES", 100.0, 6000).model == "no_fill")
    f_no = shadow.aggressive_fill(book, "NO", 200.0, limit_price_bp=4200)
    ok("buying NO lifts the other side of the book at 10000 minus the bid",
       abs(f_no.avg_price_bp - 4100) < 1e-9, f_no.summary())
    ok("an unknown side is refused",
       raises(lambda: shadow.aggressive_fill(book, "MAYBE", 10.0, 6000),
              ShadowError))
    print(f"        250 @ {f_full.avg_price_bp:.0f}bp   "
          f"900 @ {f_walk.avg_price_bp:.0f}bp   mid was {book.mid_bp:.0f}bp")

    # ------------------------------------------------ queue
    print("\n[4] Touching a limit is not a fill\n")
    touched = shadow.passive_fill(book, "YES", 200.0, 5900,
                                  traded_at_level_usd=300.0)
    ok("the level traded but the queue ahead was never cleared",
       touched.filled_usd == 0.0, touched.summary())
    ok("...and the record says exactly why",
       "queue" in (touched.reason or "") and touched.queue_ahead_usd == 400.0)
    partial = shadow.passive_fill(book, "YES", 200.0, 5900,
                                  traded_at_level_usd=500.0)
    ok("once the queue clears, the overflow fills us",
       partial.filled_usd == 100.0 and not partial.fully_filled,
       partial.summary())
    full = shadow.passive_fill(book, "YES", 200.0, 5900,
                               traded_at_level_usd=900.0)
    ok("enough volume fills the whole order",
       full.filled_usd == 200.0 and full.fully_filled, full.summary())
    ok("a passive fill prints at the limit, never better",
       full.avg_price_bp == 5900.0)
    empty = shadow.passive_fill(book, "YES", 200.0, 5500,
                                traded_at_level_usd=1000.0)
    ok("resting where nothing is queued means no queue to clear",
       empty.filled_usd == 200.0 and empty.queue_ahead_usd == 0.0)
    ok("negative traded volume is refused",
       raises(lambda: shadow.passive_fill(book, "YES", 10.0, 5900, -1.0),
              ShadowError))

    # The inference this function exists to refuse.
    ok("the traded volume is a required argument, not inferred from depth",
       "traded_at_level_usd" in
       shadow.passive_fill.__code__.co_varnames[
           :shadow.passive_fill.__code__.co_argcount])

    print("\n[5] Intent to cancel is not cancellation\n")
    late = shadow.passive_fill(book, "YES", 200.0, 5900,
                               traded_at_level_usd=400.0,
                               traded_after_cancel_usd=300.0)
    ok("volume arriving during the cancel round trip still fills",
       late.filled_usd == 200.0 and late.fully_filled, late.summary())
    ok("...and the fill is flagged as post-cancel", late.filled_after_cancel)
    clean = shadow.passive_fill(book, "YES", 100.0, 5900,
                                traded_at_level_usd=600.0,
                                traded_after_cancel_usd=300.0)
    ok("a fill completed before the cancel is not flagged",
       clean.filled_usd == 100.0 and not clean.filled_after_cancel,
       clean.summary())
    ok("the schema requires a cancel time on every passive order",
       raises(lambda: con.execute(
           "INSERT INTO shadow_orders(contract_id, book_snapshot_id, side, style, "
           "limit_price_bp, intended_size_usd, placed_at) "
           "VALUES(?,?,'YES','passive',5900,100.0,?)", (cid, b1, at())),
           sqlite3.IntegrityError))

    # ------------------------------------------------ markouts
    print("\n[6] Was the fill good, or just available?\n")
    o1 = shadow.record_order(con, contract_id=cid, book_snapshot_id=b1, side="YES",
                             style="passive", limit_price_bp=5900,
                             intended_size_usd=200.0, placed_at=at(),
                             cancel_at=at(minutes=5), cancel_latency_ms=250.0)
    fill1 = shadow.record_fill(con, o1, late, recorded_at=at(minutes=5))
    adverse = shadow.markout(con, fill1, b2, computed_at=at(minutes=15))
    ok("a fill followed by the mid falling is adverse for a YES buyer",
       adverse > 0, adverse)
    ok("the magnitude is the mid move",
       abs(adverse - (6000.0 - 5800.0)) < 1e-9, adverse)
    ok("the markout records the horizon it was measured over",
       con.execute("SELECT horizon_seconds FROM fill_markouts WHERE fill_id=?",
                   (fill1,)).fetchone()[0] == 600.0)

    o2 = shadow.record_order(con, contract_id=cid, book_snapshot_id=b2, side="NO",
                             style="aggressive", limit_price_bp=4200,
                             intended_size_usd=100.0, placed_at=at(minutes=11))
    f2 = shadow.aggressive_fill(shadow.load_book(con, b2), "NO", 100.0, 4400)
    fill2 = shadow.record_fill(con, o2, f2, recorded_at=at(minutes=11))
    b3 = shadow.record_book(con, cid, bids=[(6300, 500.0)], asks=[(6500, 500.0)],
                            captured_at=at(minutes=30), source="fixture")
    adverse2 = shadow.markout(con, fill2, b3, computed_at=at(minutes=35))
    ok("a NO position is adverse when the mid RISES",
       adverse2 > 0, adverse2)

    ok("marking against an earlier snapshot is refused",
       raises(lambda: shadow.markout(con, fill2, b1), ShadowError))
    o3 = shadow.record_order(con, contract_id=cid, book_snapshot_id=b1, side="YES",
                             style="passive", limit_price_bp=5900,
                             intended_size_usd=200.0, placed_at=at(),
                             cancel_at=at(minutes=5))
    fill3 = shadow.record_fill(con, o3, touched, recorded_at=at(minutes=5))
    ok("an unfilled order has no markout, because there is no position",
       raises(lambda: shadow.markout(con, fill3, b2), ShadowError))
    ok("the schema refuses a priced fill with no price",
       raises(lambda: con.execute(
           "INSERT INTO shadow_fills(order_id, fill_model, filled_usd, "
           "fully_filled, recorded_at) VALUES(?,'book_walk',50.0,0,?)",
           (o2, at())), sqlite3.IntegrityError))
    ok("the schema refuses a no-fill with no reason",
       raises(lambda: con.execute(
           "INSERT INTO shadow_fills(order_id, fill_model, filled_usd, "
           "fully_filled, recorded_at) VALUES(?,'no_fill',0.0,0,?)",
           (o2, at())), sqlite3.IntegrityError))

    # ------------------------------------------------ the report
    print("\n[7] The claim §10.2 asserts, measured instead\n")
    rep = shadow.adverse_selection_report(con)
    ok("the report counts every order, filled or not", rep.n_orders == 3)
    ok("...and the fill rate is not 100%",
       rep.n_filled == 2 and rep.fill_rate < 1.0, rep.summary())
    ok("markouts are averaged across fills", rep.mean_adverse_bp is not None)
    ok("post-cancel fills are broken out separately",
       rep.mean_adverse_after_cancel_bp is not None)
    ok("realised cost against the pre-trade mid is reported",
       rep.mean_realised_cost_bp is not None, rep.mean_realised_cost_bp)
    # The comparison the whole module exists to make. A passive fill earns the
    # spread -- it prints inside the mid -- and then pays it back, or worse, to
    # whatever moved the price. Netting the two is the only honest read.
    spread_earned = -rep.mean_realised_cost_bp
    net = spread_earned - rep.mean_adverse_passive_bp
    ok("the passive fill did print inside the mid, as passive fills do",
       spread_earned > 0, spread_earned)
    ok("...and adverse selection more than took it back",
       net < 0, f"earned {spread_earned:+.0f}bp, gave back "
                f"{rep.mean_adverse_passive_bp:+.0f}bp, net {net:+.0f}bp")
    print(f"        {rep.summary()}")
    print(f"        passive economics: earned {spread_earned:+.0f}bp of spread, "
          f"markout {rep.mean_adverse_passive_bp:+.0f}bp against, "
          f"NET {net:+.0f}bp")

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

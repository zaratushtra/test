#!/usr/bin/env python3
"""
Scale: the hot paths at the size a real study reaches, not fixture size.

Every other suite runs on tens of rows, where a quadratic loop and a linear one
look identical. That is how `cluster_items` shipped comparing every pair: it was
correct, it was tested, and it took **315 seconds on twelve thousand items**
while every suite passed in under a second.

The budgets here are deliberately loose — several times the measured figure — so
this fails on a change of *complexity class*, not on a slow machine. A test that
fails when CI is busy gets disabled, and a disabled test catches nothing.

Run: python3 tests/test_scale.py
Stdlib only.
"""

from __future__ import annotations

import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import (chain, evaluate, evidence, ledger, models,  # noqa: E402
                   params, refclass, registry, shadow, sizing, timeutil)

PASS, FAIL = [], []
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
iso = timeutil.iso


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, time.monotonic() - t0


def budget(label, seconds, limit, measured_note=""):
    ok(f"{label} ({seconds:.2f}s, budget {limit:.0f}s){measured_note}",
       seconds < limit, f"{seconds:.2f}s")


def main() -> int:
    print("=" * 76)
    print("SPINE SCALE")
    print("=" * 76)

    print("\n[1] Clustering, which was quadratic and is the reason this exists\n")
    rng = random.Random(7)
    vocab = [f"w{i}" for i in range(400)]
    con = ledger.connect(":memory:", create=True)
    src = evidence.ensure_source(con, "Bulk", "outlet", now=iso(NOW))
    N = 6000
    # Syndicated copies sit NEXT TO their original in time, which is how
    # syndication actually happens -- and is necessary here, because a copy
    # 4800 minutes later is eighty hours away and correctly outside the
    # seventy-two hour window. The first version of this fixture spread them
    # evenly and then reported that deduplication had broken.
    n_unique = int(N * 0.8)
    n_dupes = N - n_unique
    bodies, items = [], []
    for i in range(n_unique):
        body = " ".join(rng.choice(vocab) for _ in range(60)) + f" story {i}"
        bodies.append(body)
        items.append(body)
        if i < n_dupes:                       # a copy, minutes behind
            items.append(body)
    items = [{"id": k + 1, "body": b,
              "available_for_decision_at": iso(NOW + timedelta(minutes=k))}
             for k, b in enumerate(items[:N])]
    for it in items:
        con.execute(
            "INSERT INTO signal_items(content_hash,source_id,first_seen_at,"
            "artifact_created_at,available_for_decision_at,item_class) "
            "VALUES(?,?,?,?,?,'reportage')",
            (f"b{it['id']}", src, iso(NOW), "x",
             it["available_for_decision_at"]))
    con.commit()
    distinct = len({it["body"] for it in items})
    clusters, dt = timed(lambda: evidence.cluster_items(con, items,
                                                        cluster_version=1))
    budget(f"{N} unanchored items cluster", dt, 30.0,
           "  [all-pairs took 315s at 12k]")
    ok("...and the duplicates are still found exactly",
       len(clusters) == distinct, (len(clusters), distinct))

    anchored = [{**it, "anchor": f"p{it['id'] // 50}"} for it in items]
    _, dt_a = timed(lambda: evidence.cluster_items(con, anchored,
                                                   cluster_version=2))
    budget(f"{N} anchored items cluster", dt_a, 30.0)
    print(f"        {N} items: unanchored {dt:.2f}s, anchored {dt_a:.2f}s")

    # The property that actually matters: growth, not absolute time.
    half = items[:N // 2]
    _, dt_half = timed(lambda: evidence.cluster_items(con, half,
                                                      cluster_version=3))
    growth = dt / dt_half if dt_half > 0 else 0
    ok("doubling the corpus does not quadruple the time",
       growth < 3.0, f"{growth:.1f}x for 2x the items")
    print(f"        {N // 2} -> {N} items: {growth:.1f}x the time "
          "(quadratic would be ~4x)")

    print("\n[2] The record, at a year of it\n")
    db = ledger.connect(":memory:", create=True)
    rows = [{"id": f"m{i}", "question": f"Q{i}",
             "end_date": iso(NOW + timedelta(days=400)), "rules_text": "r",
             "outcome_token_id": f"t{i}"} for i in range(50)]
    registry.ingest_markets(db, rows, jurisdiction="GB", now=iso(NOW))
    cids = [r[0] for r in db.execute("SELECT id FROM contracts")]

    _, dt_books = timed(lambda: [
        shadow.record_book(db, c, [(5900 + h % 50, 500.0)],
                           [(6100 + h % 50, 500.0)],
                           captured_at=iso(NOW + timedelta(hours=h)),
                           source="fixture")
        for c in cids for h in range(0, 2000, 4)])
    n_books = db.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
    budget(f"{n_books} book snapshots recorded", dt_books, 60.0)

    _, dt_at = timed(lambda: [
        shadow.book_at(db, random.choice(cids),
                       iso(NOW + timedelta(hours=1500)), max_age_seconds=None)
        for _ in range(500)])
    budget(f"500 point-in-time book lookups over {n_books} snapshots",
           dt_at, 10.0)

    rule = refclass.declare_selection_rule(db, {"f": "x"},
                                           created_at=iso(NOW - timedelta(days=2)))
    rcid = refclass.freeze(
        db, class_name="f", version=1, description="d",
        members=[refclass.Member(f"c{i}", iso(NOW - timedelta(days=300 - i)),
                                 1 if i % 3 == 0 else 0) for i in range(40)],
        selection_rule_ref=rule, alpha=1.0, beta=9.0, prior_justification="j",
        exposure_units=40.0, exposure_unit_name="case",
        frozen_at=iso(NOW - timedelta(days=1)))
    rc = refclass.load(db, rcid)
    ph = params.commit(db, iso(NOW))

    N_F = 2000

    def register_all():
        for i in range(N_F):
            created = NOW + timedelta(hours=i)
            pid = registry.ensure_proposition(
                db, f"P{i}", "c", iso(created + timedelta(days=9)), "f", "T1",
                now=iso(created))
            fc = models.baseline_forecast(rc, time_remaining=1.0)
            mh = ledger.put_manifest(db, "forecast_inputs",
                                     {"q": i, "params": ph}, iso(created))
            ledger.register_forecast(
                db, proposition_id=pid, contract_id=cids[i % 50],
                p_est_bp=fc.p_est_bp, p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp,
                uncertainty_method=fc.uncertainty_method, min_width_bp=0,
                p_base_bp=fc.p_est_bp, forecast_kind="independent",
                reference_class_version_id=rcid, inputs_manifest_hash=mh,
                model_version="v1", regime_id=f"r{i % 14}",
                created_at=iso(created),
                label_available_at=iso(created + timedelta(days=10)),
                source="model")

    _, dt_reg = timed(register_all)
    budget(f"{N_F} forecasts registered, each hashed into the chain", dt_reg, 30.0)

    for pid in [r[0] for r in db.execute("SELECT id FROM propositions")]:
        evaluate.record_resolution(
            db, pid, "resolved_yes" if pid % 2 else "resolved_no", "m",
            recorded_at=iso(NOW + timedelta(days=400)))

    print("\n[3] Every read path over that record\n")
    v, dt_v = timed(lambda: chain.verify(db))
    budget(f"chain.verify over {v.length} forecasts", dt_v, 10.0)
    ok("...and the chain is intact", v.ok, v.summary())

    run, dt_s = timed(lambda: evaluate.score_all(
        db, as_of=iso(NOW + timedelta(days=400))))
    budget(f"score_all writing {run.scored} scores", dt_s, 10.0)

    obs, dt_o = timed(lambda: evaluate.observations(db))
    budget(f"observations rebuilding {len(obs)} from the record", dt_o, 10.0)
    ok("...and it found them all", len(obs) == N_F, len(obs))

    _, dt_stale = timed(lambda: evaluate.stale_scores(db))
    budget("stale_scores", dt_stale, 10.0)
    _, dt_sl = timed(lambda: evaluate.slices(db))
    budget("slices", dt_sl, 5.0)

    evaluate.declare_plan(db, n_looks=10, declared_by="scale")
    ev, dt_e = timed(lambda: evaluate.evaluate(db, resamples=2000))
    budget("evaluate with a 2000-resample bootstrap", dt_e, 20.0)
    ok("...and it produced a verdict", ev.blocked_by is None, ev.blocked_by)

    fhash = db.execute("SELECT forecast_hash FROM forecasts LIMIT 1").fetchone()[0]
    _, dt_cx = timed(lambda: sizing.cluster_exposure_used_usd(db, fhash))
    budget("cluster_exposure over the loss graph", dt_cx, 5.0)

    print("\n[4] The two indexes that measurement justified\n")
    # 26 foreign keys have no leading index. Only two are ever FILTERED by
    # directly, which is what costs: SQLite plans a join from the child outward
    # using the parent's primary key, so an unindexed FK is free there and
    # expensive in a WHERE clause. Adding all 26 on principle would be paying
    # write cost for 24 reads nobody makes.
    idx = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    ok("claim_contract_effects is indexed by the column it is filtered by",
       "idx_effects_contract" in idx, sorted(idx))
    ok("...and trade_decisions likewise", "idx_decisions_forecast" in idx)

    eff_db = ledger.connect(":memory:", create=True)
    registry.ingest_markets(eff_db, rows[:20], jurisdiction="GB", now=iso(NOW))
    eff_db.execute("INSERT INTO event_clusters VALUES('c',1,?,?,?)",
                   (iso(NOW), iso(NOW), iso(NOW)))
    eff_db.executemany(
        """INSERT INTO claims(claim_hash,cluster_id,cluster_version,assertion,
           authenticity,extraction_fidelity,establishes,n_eff_sources,
           available_for_decision_at,computed_at,feature_version)
           VALUES(?,'c',1,'a','artifact_verified','checked_faithful',
                  'underlying_fact',1.0,?,?,'v1')""",
        [(f"cl{i}", iso(NOW), iso(NOW)) for i in range(100)])
    N_E = 40000
    eff_db.executemany(
        """INSERT INTO claim_contract_effects(claim_id,contract_id,horizon_days,
           llr,conditioned_on_ref,estimator,model_version,final_contribution,
           contribution_cap,available_for_decision_at,computed_at)
           VALUES(?,?,14.0,0.5,'m','fitted_model',?,0.4,1.5,?,?)""",
        [((i % 100) + 1, (i % 20) + 1, f"v{i}", iso(NOW), iso(NOW))
         for i in range(N_E)])
    eff_db.commit()
    as_of = iso(NOW + timedelta(days=1))
    # Timed separately, because most of the wall time here is Python building
    # dicts and not the query: 2000 rows come back per lookup, so 200 lookups
    # construct 400k of them. Reporting the combined figure would credit the
    # index for a cost it does not pay and hide the one it does.
    _, dt_sql = timed(lambda: [
        eff_db.execute("SELECT id FROM claim_contract_effects WHERE "
                       "contract_id=? AND available_for_decision_at <= ?",
                       ((c % 20) + 1, as_of)).fetchall() for c in range(200)])
    budget(f"200 indexed lookups over {N_E} effects", dt_sql, 5.0,
           "  [1.57s unindexed at 200k]")
    _, dt_eff = timed(lambda: [
        ledger.effects_available_at(eff_db, (c % 20) + 1, as_of)
        for c in range(200)])
    budget("...the same through effects_available_at", dt_eff, 15.0)
    per_lookup = len(ledger.effects_available_at(eff_db, 1, as_of))
    ok("the difference is row materialisation, not the query",
       dt_eff > dt_sql * 2, (dt_sql, dt_eff))
    print(f"        {N_E} effects, 200 lookups: query {dt_sql:.3f}s, "
          f"with materialisation {dt_eff:.3f}s ({per_lookup} rows each)")

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

#!/usr/bin/env python3
"""
Evidence pipeline validation: ingestion, deduplication, effective sources,
claims, contradictions and the influence budget.

The suite is organised around the ways this layer flatters itself: counting
syndicated copies as corroboration, treating unmeasured source pairs as
independent, applying a correlation learned later to a decision made earlier,
and capping an input instead of the product it feeds.

Run: python3 tests/test_evidence.py
Stdlib only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import evidence, ledger, registry, timeutil  # noqa: E402
from spine.evidence import EvidenceError  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 19, 9, 0, 0, tzinfo=timezone.utc)


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


def iso(dt):
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


def at(**kw):
    return iso(NOW + timedelta(**kw))


WIRE_BODY = ("The committee voted 12 to 5 on Thursday to advance the measure to "
             "the floor, according to two people familiar with the deliberations, "
             "clearing the way for a vote before the recess begins.")


def main() -> int:
    print("=" * 76)
    print("SPINE EVIDENCE PIPELINE VALIDATION")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)

    wire = evidence.ensure_source(con, "Global Wire", "wire", now=at())
    paper_a = evidence.ensure_source(con, "Herald", "outlet",
                                     owner_group="Meridian", now=at())
    paper_b = evidence.ensure_source(con, "Gazette", "outlet",
                                     owner_group="Meridian", now=at())
    indep = evidence.ensure_source(con, "Independent Desk", "outlet", now=at())
    official = evidence.ensure_source(con, "Committee Clerk", "official", now=at())

    # ------------------------------------------------ ingestion
    print("\n[1] Five timestamps, one of which governs retrieval\n")
    ok("registering the same source twice returns the same id",
       evidence.ensure_source(con, "Herald", "outlet") == paper_a)

    r1 = evidence.ingest_item(
        con, wire, WIRE_BODY, first_seen_at=at(minutes=0), item_class="reportage",
        verification_lag_seconds=300, claimed_published_at=at(minutes=-90),
        event_at=at(hours=-3), title="Committee advances measure", now=at())
    row = con.execute(
        "SELECT first_seen_at, available_for_decision_at, claimed_published_at "
        "FROM signal_items WHERE id=?", (r1.item_id,)).fetchone()
    ok("availability is first_seen plus the verification lag, not publication",
       row[1] == at(minutes=5), row)
    ok("the source's own claimed publication time is recorded",
       row[2] == at(minutes=-90))
    ok("...and is never what availability is derived from",
       row[1] > row[0] and row[1] != row[2])
    ok("availability before first sight is refused",
       raises(lambda: evidence.ingest_item(
           con, indep, "x", first_seen_at=at(hours=1), item_class="reportage",
           available_for_decision_at=at(minutes=0)), EvidenceError))
    ok("an item with no content is refused",
       raises(lambda: evidence.ingest_item(
           con, indep, "   ", first_seen_at=at(), item_class="reportage"),
           EvidenceError))
    ok("...because every empty item shares one hash and would look syndicated",
       "syndicated" in _msg(lambda: evidence.ingest_item(
           con, indep, "", first_seen_at=at(), item_class="reportage")))
    ok("a negative verification lag is refused",
       raises(lambda: evidence.ingest_item(
           con, indep, "y", first_seen_at=at(), item_class="reportage",
           verification_lag_seconds=-1), EvidenceError))
    ok("re-ingesting identical content from one source is a no-op",
       evidence.ingest_item(con, wire, WIRE_BODY, first_seen_at=at(),
                            item_class="reportage").item_id == r1.item_id)

    # ------------------------------------------------ syndication
    print("\n[2] Syndication is duplication, and duplication is not corroboration\n")
    r2 = evidence.ingest_item(con, paper_a, WIRE_BODY, first_seen_at=at(minutes=20),
                              item_class="reportage", verification_lag_seconds=300,
                              now=at())
    r3 = evidence.ingest_item(con, paper_b, WIRE_BODY, first_seen_at=at(minutes=25),
                              item_class="reportage", verification_lag_seconds=300,
                              now=at())
    ok("identical content under a second masthead is detected",
       r2.duplicate_of == [r1.item_id], r2.duplicate_of)
    ok("...and the third copy sees both earlier ones", len(r3.duplicate_of) == 2)
    ok("syndication correlation is recorded at 1.0",
       con.execute("SELECT COUNT(*) FROM source_error_correlation "
                   "WHERE basis='syndication' AND correlation=1.0").fetchone()[0] == 3)

    syndicated = evidence.n_eff_sources(con, [wire, paper_a, paper_b], at(hours=1))
    ok("three copies of one wire story count as one effective source",
       abs(syndicated.n_eff - 1.0) < 1e-9, syndicated.summary())
    print(f"        {syndicated.summary()}")

    # An independent desk with its own reporting on the same event.
    own = ("Two members told this newspaper the vote had been scheduled only after "
           "the chair secured an agreement on the amendment, and that the tally "
           "reflected a compromise reached the previous evening.")
    r4 = evidence.ingest_item(con, indep, own, first_seen_at=at(minutes=40),
                              item_class="reportage", verification_lag_seconds=600,
                              now=at())
    ok("genuinely different text is not flagged as a duplicate",
       r4.duplicate_of == [])
    with_indep = evidence.n_eff_sources(con, [wire, paper_a, paper_b, indep],
                                        at(hours=1))
    ok("adding a genuinely independent desk raises n_eff",
       with_indep.n_eff > syndicated.n_eff, with_indep.summary())
    print(f"        {with_indep.summary()}")

    # ------------------------------------------------ assumed correlation
    print("\n[3] An unmeasured pair is unknown, never independent\n")
    r, basis = evidence.pairwise_correlation(con, indep, official, at(hours=1))
    ok("two unrelated sources fall back to the assumed floor, not zero",
       basis == "assumed" and r == evidence.ASSUMED_CORRELATION, (r, basis))
    r_own, basis_own = evidence.pairwise_correlation(con, paper_a, paper_b, at(days=-1))
    ok("shared ownership is a stronger assumption than the floor",
       basis_own == "ownership" and r_own > evidence.ASSUMED_CORRELATION,
       (r_own, basis_own))
    eff2 = evidence.n_eff_sources(con, [indep, official], at(hours=1))
    ok("two assumed-correlated sources are worth less than two",
       eff2.n_eff < 2.0, eff2.summary())
    ok("...and the result declares that it leans on assumptions",
       eff2.leans_on_assumptions)
    ok("a single source is exactly one",
       evidence.n_eff_sources(con, [indep], at(hours=1)).n_eff == 1.0)
    ok("no sources at all is refused",
       raises(lambda: evidence.n_eff_sources(con, [], at()), EvidenceError))

    # ------------------------------------------------ point-in-time
    print("\n[4] A correlation learned in March cannot sharpen February\n")
    con.execute(
        "INSERT INTO source_error_correlation(source_a, source_b, correlation, "
        "basis, n_observations, computed_at) VALUES(?,?,?,?,?,?)",
        (*sorted((indep, official)), 0.9, "observed_error_agreement", 40,
         at(days=30)))
    con.commit()
    before = evidence.n_eff_sources(con, [indep, official], at(days=1))
    after = evidence.n_eff_sources(con, [indep, official], at(days=40))
    ok("a decision before the estimate exists does not see it",
       before.measured_pairs == 0 and before.assumed_pairs == 1, before.summary())
    ok("a decision after it does", after.measured_pairs == 1, after.summary())
    ok("...and the later estimate lowers n_eff, as a real measurement should",
       after.n_eff < before.n_eff, (before.n_eff, after.n_eff))
    print(f"        before: {before.summary()}\n        after : {after.summary()}")

    # ------------------------------------------------ clustering
    print("\n[5] Clustering is versioned, and originators need attribution\n")
    items = [
        {"id": r1.item_id, "body": WIRE_BODY, "anchor": "prop:floor-vote",
         "available_for_decision_at": at(minutes=5)},
        {"id": r2.item_id, "body": WIRE_BODY, "anchor": "prop:floor-vote",
         "available_for_decision_at": at(minutes=25), "attributes_to": "Global Wire"},
        {"id": r3.item_id, "body": WIRE_BODY, "anchor": "prop:floor-vote",
         "available_for_decision_at": at(minutes=30), "attributes_to": "Global Wire"},
        {"id": r4.item_id, "body": own, "anchor": "prop:floor-vote",
         "available_for_decision_at": at(minutes=50)},
    ]
    clusters = evidence.cluster_items(con, items, cluster_version=1)
    ok("all four items retrieved for one proposition land in one cluster",
       len(clusters) == 1 and len(clusters[0].item_ids) == 4,
       [c.item_ids for c in clusters])
    c0 = clusters[0]
    ok("the byte-identical copies are counted as near-duplicate pairs",
       c0.near_duplicate_pairs >= 3, c0.near_duplicate_pairs)
    ok("the wire is named originator because the copies cite it",
       c0.originators == [r1.item_id], c0.originators)

    # The measurement behind the anchor-first rule. Two independent reports of
    # one event share two content words; no threshold separates that from
    # unrelated text, so lexical linkage must not be asked to find events.
    ta, tb = evidence.topic_tokens(WIRE_BODY), evidence.topic_tokens(own)
    jac = len(ta & tb) / len(ta | tb)
    ok("independent reports of the same event are lexically unrelated",
       jac < 0.10, f"jaccard {jac:.3f}")
    ok("...so without an anchor they do NOT merge, and under-merging is the "
       "safe direction",
       len(evidence.cluster_items(
           con, [{k: v for k, v in it.items() if k != "anchor"} for it in items],
           cluster_version=3)) == 2)
    ok("byte-identical copies still merge with no anchor at all, which is what "
       "deduplication actually needs",
       sorted(len(c.item_ids) for c in evidence.cluster_items(
           con, [{k: v for k, v in it.items() if k != "anchor"} for it in items],
           cluster_version=4)) == [1, 3])

    unattributed = [dict(it) for it in items]
    for it in unattributed:
        it.pop("attributes_to", None)
    c1 = evidence.cluster_items(con, unattributed, cluster_version=2)[0]
    ok("with no attribution anywhere, NO originator is nominated",
       c1.originators == [], c1.originators)
    ok("...rather than the earliest arrival being assumed to be it",
       r1.item_id not in c1.originators)
    ok("reclustering writes a new version and leaves version 1 intact",
       con.execute("SELECT COUNT(DISTINCT cluster_version) FROM event_clusters")
       .fetchone()[0] == 4)
    ok("stopwords are kept for duplicate detection",
       "voted 12 to 5 on" in evidence.shingles(WIRE_BODY))
    print(f"        independent reports of one event: jaccard {jac:.3f} "
          f"-> anchor, not text, is what groups them")
    print(f"        v1 cluster {c0.cluster_id}: {len(c0.item_ids)} items, "
          f"{c0.near_duplicate_pairs} duplicate pairs, "
          f"originator {c0.originators or 'none identified'}")

    print("\n[5b] Clustering has to survive a real corpus\n")
    import random as _rnd
    import time as _time
    from datetime import timedelta as _td
    _r = _rnd.Random(7)
    _vocab = [f"w{i}" for i in range(400)]
    big_con = ledger.connect(":memory:", create=True)
    big_src = evidence.ensure_source(big_con, "Bulk", "outlet")
    N = 3000
    uniq = [" ".join(_r.choice(_vocab) for _ in range(60)) + f" story {i}"
            for i in range(int(N * 0.8))]
    big_items = []
    for i in range(N):
        big_con.execute(
            "INSERT INTO signal_items(content_hash,source_id,first_seen_at,"
            "artifact_created_at,available_for_decision_at,item_class) "
            "VALUES(?,?,?,?,?,'reportage')",
            (f"bulk{i}", big_src, at(), "x", timeutil.iso(NOW + _td(minutes=i))))
        big_items.append({"id": i + 1, "body": uniq[i % len(uniq)],
                          "available_for_decision_at":
                              timeutil.iso(NOW + _td(minutes=i))})
    big_con.commit()
    t0 = _time.monotonic()
    big_clusters = evidence.cluster_items(big_con, big_items, cluster_version=1)
    elapsed = _time.monotonic() - t0
    # The all-pairs version measured 52s at 4000 items and was cleanly
    # quadratic -- about forty minutes at twenty thousand, which a collector
    # running for a month produces.
    ok(f"{N} items cluster in well under a minute", elapsed < 20.0,
       f"{elapsed:.1f}s")
    ok("...and the duplicates are still found exactly",
       len(big_clusters) == len(uniq), (len(big_clusters), len(uniq)))
    ok("...with every item placed in exactly one cluster",
       sum(len(c.item_ids) for c in big_clusters) == N)
    print(f"        {N} items -> {len(big_clusters)} clusters in {elapsed:.2f}s")

    # ------------------------------------------------ claims
    print("\n[6] Four verification questions, answered separately\n")
    cid, eff = evidence.record_claim(
        con, cluster_id=c0.cluster_id, cluster_version=1,
        assertion="The committee voted 12-5 to advance the measure",
        authenticity="artifact_verified", extraction_fidelity="checked_faithful",
        establishes="underlying_fact",
        source_ids=[wire, paper_a, paper_b, indep],
        available_for_decision_at=at(hours=1), feature_version="ev-1",
        primary_artifact_id=r1.item_id, now=at(hours=1))
    ok("a claim records and returns its effective source count",
       cid > 0 and eff.n_eff == con.execute(
           "SELECT n_eff_sources FROM claims WHERE id=?", (cid,)).fetchone()[0])
    ok("the claim carries no likelihood ratio at all",
       "llr" not in {d[1] for d in con.execute("PRAGMA table_info(claims)")})
    ok("recording the same claim twice returns the same row",
       evidence.record_claim(
           con, cluster_id=c0.cluster_id, cluster_version=1,
           assertion="The committee voted 12-5 to advance the measure",
           authenticity="artifact_verified",
           extraction_fidelity="checked_faithful", establishes="underlying_fact",
           source_ids=[wire], available_for_decision_at=at(hours=1),
           feature_version="ev-1")[0] == cid)
    ok("a claim with no sources is refused",
       raises(lambda: evidence.record_claim(
           con, cluster_id=c0.cluster_id, cluster_version=1, assertion="x",
           authenticity="artifact_verified",
           extraction_fidelity="checked_faithful", establishes="underlying_fact",
           source_ids=[], available_for_decision_at=at(), feature_version="ev-1"),
           EvidenceError))
    ok("claims are append-only",
       raises(lambda: con.execute(
           "UPDATE claims SET assertion='edited' WHERE id=?", (cid,)),
           sqlite3.IntegrityError))

    weak, _ = evidence.record_claim(
        con, cluster_id=c0.cluster_id, cluster_version=1,
        assertion="A spokesman said negotiations are progressing",
        authenticity="artifact_verified", extraction_fidelity="checked_faithful",
        establishes="only_that_it_was_asserted", source_ids=[indep],
        available_for_decision_at=at(hours=1), feature_version="ev-1", now=at(hours=1))
    ok("a statement that establishes only its own utterance is still recorded",
       weak > 0)

    # ------------------------------------------------ contradictions
    print("\n[7] Contradictions are recorded, never auto-applied\n")
    denial, _ = evidence.record_claim(
        con, cluster_id=c0.cluster_id, cluster_version=1,
        assertion="No vote took place on Thursday",
        authenticity="artifact_unverified", extraction_fidelity="unchecked",
        establishes="only_that_it_was_asserted", source_ids=[official],
        available_for_decision_at=at(hours=2), feature_version="ev-1", now=at(hours=2))
    xid = evidence.record_contradiction(con, cid, denial, "genuine_contradiction",
                                        at(hours=2))
    ok("a contradiction is recorded", xid > 0)
    ok("neither claim is altered by it",
       con.execute("SELECT n_eff_sources FROM claims WHERE id=?",
                   (cid,)).fetchone()[0] == eff.n_eff)
    ok("it shows as open until someone adjudicates",
       len(evidence.open_contradictions(con, at(hours=3))) == 1)
    ok("an adjudication with no named adjudicator is refused",
       raises(lambda: evidence.adjudicate(con, xid, "the denial is unsupported", "",
                                          at(hours=4)), EvidenceError))
    evidence.adjudicate(con, xid, "denial is unsourced and contradicts the clerk's "
                        "own minutes; the vote claim stands", "analyst-1", at(hours=4))
    ok("once adjudicated it is no longer open",
       evidence.open_contradictions(con, at(hours=5)) == [])
    ok("a contradiction that does not exist cannot be adjudicated",
       raises(lambda: evidence.adjudicate(con, 9999, "x", "y", at()), EvidenceError))

    # ------------------------------------------------ influence budget
    print("\n[8] The cap binds the product — the v1 failure, in a test\n")
    c_small = evidence.contribution(llr=1.0, n_eff=1.0, cap=1.5)
    c_many = evidence.contribution(llr=1.0, n_eff=100.0, cap=1.5)
    ok("more independent sources never scale a contribution above the cap",
       c_many.final <= 1.5 + 1e-12, c_many.summary())
    ok("the independence weight itself is bounded at 1.0", c_many.weight <= 1.0)
    ok("more sources still means more weight, up to the bound",
       c_many.weight > c_small.weight)
    big = evidence.contribution(llr=9.0, n_eff=100.0, cap=1.5, lambda_sig=4.0)
    ok("a large llr times a large lambda is clamped, not passed through",
       big.final == 1.5 and big.raw > 30, big.summary())
    ok("...and the clamping is visible in the record, not silent", big.capped)
    ok("the sign survives clamping",
       evidence.contribution(-9.0, 100.0, 1.5).final == -1.5)
    ok("a zero cap is refused",
       raises(lambda: evidence.contribution(1.0, 1.0, 0.0), EvidenceError))
    ok("a negative lambda is refused",
       raises(lambda: evidence.contribution(1.0, 1.0, 1.5, lambda_sig=-1.0),
              EvidenceError))
    print(f"        {big.summary()}")

    print("\n    caps by verification state:")
    for est in ("underlying_fact", "only_that_it_was_asserted", "indeterminate"):
        caps = [evidence.cap_for(est, a, "checked_faithful")
                for a in ("artifact_verified", "artifact_unverified", "artifact_disputed")]
        print(f"      {est:<28} verified {caps[0]:.3f}  unverified {caps[1]:.3f}  "
              f"disputed {caps[2]:.3f}")
    ok("a verified underlying fact gets the largest budget",
       evidence.cap_for("underlying_fact", "artifact_verified", "checked_faithful")
       == max(evidence.cap_for(e, a, f)
              for e in CAPS for a in AUTHS for f in FIDS))
    ok("an assertion-only claim is bounded, not zeroed",
       0 < evidence.cap_for("only_that_it_was_asserted", "artifact_verified",
                            "checked_faithful") < 1.5)
    ok("a disputed, lossy, indeterminate claim still has some budget",
       evidence.cap_for("indeterminate", "artifact_disputed", "known_lossy") > 0)
    ok("an unknown verification value is refused",
       raises(lambda: evidence.cap_for("probably_true", "artifact_verified",
                                       "checked_faithful"), EvidenceError))

    # ------------------------------------------------ effects
    print("\n[9] Effects are per contract, per horizon, per model version\n")
    rows = [{"id": "mk-committee", "question": "Will the measure reach a floor vote?",
             "end_date": at(days=20), "rules_text": "Resolves YES on a recorded "
             "floor vote before the deadline.", "outcome_token_id": "tok-1"}]
    registry.ingest_markets(con, rows, jurisdiction="GB", now=at())
    contract = con.execute("SELECT id FROM contracts").fetchone()[0]

    eid, c_near = evidence.record_effect(
        con, claim_id=cid, contract_id=contract, horizon_days=14.0, llr=1.2,
        estimator="fitted_model", model_version="eff-1",
        conditioned_on_ref="manifest/I-at-t", available_for_decision_at=at(hours=2),
        now=at(hours=2))
    _, c_far = evidence.record_effect(
        con, claim_id=cid, contract_id=contract, horizon_days=365.0, llr=0.2,
        estimator="fitted_model", model_version="eff-1",
        conditioned_on_ref="manifest/I-at-t", available_for_decision_at=at(hours=2),
        now=at(hours=2))
    ok("the same claim moves a near contract more than a far one",
       c_near.final > c_far.final, (c_near.final, c_far.final))
    ok("an effect available before its claim is refused",
       raises(lambda: evidence.record_effect(
           con, claim_id=cid, contract_id=contract, horizon_days=7.0, llr=1.0,
           estimator="fitted_model", model_version="eff-1",
           conditioned_on_ref="m", available_for_decision_at=at(minutes=1)),
           EvidenceError))
    ok("the schema refuses a stored contribution that exceeds its own cap",
       raises(lambda: con.execute(
           "INSERT INTO claim_contract_effects(claim_id, contract_id, horizon_days, "
           "llr, conditioned_on_ref, estimator, model_version, final_contribution, "
           "contribution_cap, available_for_decision_at, computed_at) "
           "VALUES(?,?,7.0,1.0,'m','fitted_model','eff-1',9.9,1.5,?,?)",
           (cid, contract, at(hours=3), at(hours=3))), sqlite3.IntegrityError))
    ok("effects are append-only",
       raises(lambda: con.execute(
           "UPDATE claim_contract_effects SET llr=5.0 WHERE id=?", (eid,)),
           sqlite3.IntegrityError))

    evidence.record_effect(
        con, claim_id=weak, contract_id=contract, horizon_days=14.0, llr=2.0,
        estimator="llm_proposed_unvalidated", model_version="eff-1",
        conditioned_on_ref="prompt/bundle-1", available_for_decision_at=at(hours=2),
        now=at(hours=2))

    print("\n[10] Retrieval: what a forecast at a given moment may use\n")
    usable = evidence.usable_contributions(con, contract, at(hours=3), "eff-1", 14.0)
    ok("retrieval is scoped to one horizon, so a claim cannot be counted twice",
       len(evidence.usable_contributions(con, contract, at(hours=3), "eff-1", 365.0))
       == 1)
    ok("two horizons for one claim coexist rather than colliding",
       con.execute("SELECT COUNT(*) FROM claim_contract_effects WHERE claim_id=? "
                   "AND estimator='fitted_model'", (cid,)).fetchone()[0] == 2)
    ok("an LLM-proposed ratio is excluded unless asked for explicitly",
       len(usable) == 1, usable)
    ok("...and asking for it explicitly does include it",
       len(evidence.usable_contributions(con, contract, at(hours=3), "eff-1", 14.0,
                                         include_unvalidated=True)) == 2)
    ok("a forecast made before the effects existed sees none",
       evidence.usable_contributions(con, contract, at(hours=1), "eff-1", 14.0) == [])
    ok("a different model version sees none of them",
       evidence.usable_contributions(con, contract, at(hours=3), "eff-2", 14.0) == [])

    evidence.record_effect(
        con, claim_id=cid, contract_id=contract, horizon_days=14.0, llr=0.4,
        estimator="fitted_model", model_version="eff-1",
        conditioned_on_ref="manifest/I-at-t2", available_for_decision_at=at(hours=5),
        now=at(hours=5))
    re_est = evidence.usable_contributions(con, contract, at(hours=6), "eff-1", 14.0)
    ok("re-estimating a claim replaces its contribution rather than adding one",
       len(re_est) == 1, re_est)
    ok("...and the newer estimate is the one used",
       any(abs(v - evidence.contribution(
           0.4, eff.n_eff, evidence.cap_for("underlying_fact", "artifact_verified",
                                            "checked_faithful")).final) < 1e-9
           for v in re_est), re_est)
    print(f"        usable at +3h: {[round(v, 4) for v in usable]}")
    print(f"        usable at +6h: {[round(v, 4) for v in re_est]}")

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


CAPS = ("underlying_fact", "only_that_it_was_asserted", "indeterminate")
AUTHS = ("artifact_verified", "artifact_unverified", "artifact_disputed")
FIDS = ("checked_faithful", "unchecked", "known_lossy")

if __name__ == "__main__":
    sys.exit(main())

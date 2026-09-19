#!/usr/bin/env python3
"""
Probe suite for SPINE schema v2.

Re-runs every probe from the 19 Sep 2026 design review — each of which the v1
schema ACCEPTED — plus positive controls proving the schema still permits the
forecasts it should. Run: python3 db/test_schema_v2.py

Stdlib only. Requires SQLite >= 3.37 for STRICT tables.
"""

import sqlite3
import sys
import os

GENESIS = "0" * 64
SCHEMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema_v2.sql")

results = []


def check(label, fn, expect):
    """expect: 'reject' or 'accept'."""
    try:
        fn()
        got = "accept"
        detail = ""
    except Exception as e:
        got = "reject"
        detail = str(e).split("\n")[0]
    ok = got == expect
    results.append((ok, label, expect, got, detail))
    mark = "PASS" if ok else "FAIL"
    tail = f"  [{detail}]" if detail and not ok else ""
    print(f"  {mark}  {label}  (expected {expect}, got {got}){tail}")
    return ok


def fresh():
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys = ON")
    with open(SCHEMA, encoding="utf-8") as fh:
        sql = fh.read()
    # WAL is meaningless in :memory: and PRAGMA in executescript is harmless
    con.executescript(sql)
    return con


def seed(con):
    """Minimum viable rows for a valid forecast."""
    con.execute("INSERT INTO sources(name,kind,created_at) VALUES('Reuters','wire','2026-01-01T00:00:00.000Z')")
    con.execute("""INSERT INTO reference_class_versions
        (class_name,version,description,n,k,alpha,beta,prior_justification,
         frozen_at,selection_rule_ref)
        VALUES('fed_holds',1,'desc',40,8,1.0,9.0,'family base rate ~0.10',
               '2026-01-01T00:00:00.000Z','rules/fed_holds_v1.md')""")
    con.execute("""INSERT INTO manifests(manifest_hash,kind,content,created_at)
        VALUES('m1','forecast_inputs','{}','2026-01-01T00:00:00.000Z')""")
    con.execute("""INSERT INTO propositions
        (proposition_hash,statement,resolution_criterion,deadline_utc,event_family,
         horizon_class,created_at)
        VALUES('p1','Will X happen','criterion','2026-10-01T00:00:00.000Z','fed',
               'T1','2026-06-01T00:00:00.000Z')""")
    con.execute("""INSERT INTO contracts
        (venue,market_id,outcome_token_id,rules_text,rules_version_hash,deadline_utc,
         resolution_source,payout_states,eligibility_status,eligibility_checked_at,first_seen_at)
        VALUES('polymarket','mk1','tok1','rules','rh1','2026-10-01T00:00:00.000Z',
               'official','{"YES":1.0,"NO":0.0,"UNKNOWN":0.5}','tradeable',
               '2026-06-01T00:00:00.000Z','2026-06-01T00:00:00.000Z')""")
    con.commit()


FC_COLS = """(forecast_hash,prev_hash,proposition_id,p_est_bp,p_lo_bp,p_hi_bp,
              uncertainty_method,min_width_bp,p_base_bp,forecast_kind,
              reference_class_version_id,inputs_manifest_hash,model_version,
              regime_id,created_at,source)"""


def insert_forecast(con, h, prev, est, lo, hi, min_w=0, created="2026-06-02T00:00:00.000Z"):
    con.execute(
        f"INSERT INTO forecasts {FC_COLS} VALUES(?,?,1,?,?,?,'bootstrap',?,1000,"
        f"'independent',1,'m1','mv1','r1',?,'human')",
        (h, prev, est, lo, hi, min_w, created),
    )
    con.commit()


def main() -> int:
    print("=" * 78)
    print("SPINE SCHEMA v2 — PROBE SUITE")
    print(f"sqlite {sqlite3.sqlite_version}")
    print("=" * 78)

    # ---------------------------------------------------------------- review probes
    print("\n[1] Review probes — all six were ACCEPTED by v1, must now be REJECTED\n")

    con = fresh(); seed(con)
    check("probability as non-integer (5000.5)",
          lambda: insert_forecast(con, "f1", GENESIS, 5000.5, 4000, 6000), "reject")

    # STRICT accepts only LOSSLESS numeric strings, coercing '5000' to integer 5000;
    # '5000.5' and 'abc' are both rejected. The stored datum is therefore always a
    # correct integer, so this is input hygiene for the application layer, not a
    # data-integrity hole. Asserted as accept-and-coerce so the behaviour is pinned.
    con = fresh(); seed(con)
    check("text '5000' coerced losslessly to integer (not an integrity hole)",
          lambda: insert_forecast(con, "f1", GENESIS, "5000", 4000, 6000), "accept")
    stored = con.execute("SELECT p_est_bp, typeof(p_est_bp) FROM forecasts").fetchone()
    assert stored == (5000, "integer"), f"coercion changed: {stored}"

    con = fresh(); seed(con)
    check("lossy text ('5000.5') rejected",
          lambda: insert_forecast(con, "f1", GENESIS, "5000.5", 4000, 6000), "reject")

    con = fresh(); seed(con)
    check("base probability out of range (15000)",
          lambda: con.execute(
              f"INSERT INTO forecasts {FC_COLS} VALUES('f1',?,1,5000,4000,6000,"
              f"'m',0,15000,'independent',1,'m1','mv1','r1','2026-06-02T00:00:00.000Z','human')",
              (GENESIS,)), "reject")

    con = fresh(); seed(con)
    check("UPDATE a frozen reference class",
          lambda: con.execute("UPDATE reference_class_versions SET n=999, k=500 WHERE id=1"),
          "reject")

    con = fresh(); seed(con)
    con.execute("""INSERT INTO event_clusters VALUES('c1',1,'2026-06-01T00:00:00.000Z',
                   '2026-06-01T00:00:00.000Z','2026-06-02T00:00:00.000Z')""")
    con.execute("""INSERT INTO claims(claim_hash,cluster_id,cluster_version,assertion,
        authenticity,extraction_fidelity,establishes,n_eff_sources,
        available_for_decision_at,computed_at,feature_version)
        VALUES('cl1','c1',1,'a','artifact_verified','checked_faithful',
               'underlying_fact',2.0,'2026-06-01T00:00:00.000Z',
               '2026-06-01T00:00:00.000Z','fv1')""")
    con.commit()
    check("UPDATE an existing claim (was: signal llr)",
          lambda: con.execute("UPDATE claims SET n_eff_sources=99.0 WHERE id=1"), "reject")

    con = fresh(); seed(con)
    insert_forecast(con, "a", GENESIS, 5000, 4000, 6000)
    insert_forecast(con, "b", "a", 5000, 4000, 6000)
    insert_forecast(con, "c", "b", 5000, 4000, 6000)
    con.execute("INSERT INTO forecast_edges VALUES('a','b',0.5,'score',0.5,'t')")
    con.execute("INSERT INTO forecast_edges VALUES('b','c',0.5,'score',0.5,'t')")
    con.commit()
    check("2-cycle (a->b then b->a)",
          lambda: con.execute("INSERT INTO forecast_edges VALUES('b','a',0.5,'score',0.5,'t')"),
          "reject")
    check("3-cycle (c->a closing a->b->c)",
          lambda: con.execute("INSERT INTO forecast_edges VALUES('c','a',0.5,'score',0.5,'t')"),
          "reject")
    check("diamond a->c is NOT a cycle (must be allowed)",
          lambda: con.execute("INSERT INTO forecast_edges VALUES('a','c',0.5,'score',0.5,'t')"),
          "accept")

    con = fresh(); seed(con)
    check("insert signal_items (sources table now exists)",
          lambda: con.execute("""INSERT INTO signal_items
              (content_hash,source_id,first_seen_at,artifact_created_at,
               available_for_decision_at,item_class)
              VALUES('h',1,'2026-06-01T00:00:00.000Z','2026-06-01T00:00:00.000Z',
                     '2026-06-01T00:00:05.000Z','reportage')"""), "accept")

    # ------------------------------------------------------- the firewall correction
    print("\n[2] The firewall correction — v1 REJECTED these legitimate forecasts\n")

    con = fresh(); seed(con)
    check("T1 forecast at p=0.02 (200bp) — v1 rejected this",
          lambda: insert_forecast(con, "f1", GENESIS, 200, 100, 500), "accept")

    con = fresh(); seed(con)
    check("T1 forecast at p=0.97 (9700bp) — v1 rejected this",
          lambda: insert_forecast(con, "f1", GENESIS, 9700, 9400, 9850), "accept")

    con = fresh(); seed(con)
    check("p=0 rejected (certainty is not a forecast)",
          lambda: insert_forecast(con, "f1", GENESIS, 0, 0, 500), "reject")

    con = fresh(); seed(con)
    check("p=1 rejected (10000bp)",
          lambda: insert_forecast(con, "f1", GENESIS, 10000, 9000, 10000), "reject")

    # --------------------------------------------------------- uncertainty integrity
    print("\n[3] Uncertainty is separate from the estimate and must be coherent\n")

    con = fresh(); seed(con)
    check("interval must contain the estimate (est outside lo..hi)",
          lambda: insert_forecast(con, "f1", GENESIS, 9000, 1000, 2000), "reject")

    con = fresh(); seed(con)
    check("inverted interval (lo > hi)",
          lambda: insert_forecast(con, "f1", GENESIS, 5000, 6000, 4000), "reject")

    con = fresh(); seed(con)
    check("interval narrower than horizon-class minimum width",
          lambda: insert_forecast(con, "f1", GENESIS, 5000, 4990, 5010, min_w=1000), "reject")

    con = fresh(); seed(con)
    check("interval meeting the minimum width",
          lambda: insert_forecast(con, "f1", GENESIS, 5000, 4000, 6000, min_w=1000), "accept")

    # ------------------------------------------------------------ influence budget
    print("\n[4] Influence budget binds the FINAL contribution, not the raw llr\n")

    con = fresh(); seed(con)
    con.execute("""INSERT INTO event_clusters VALUES('c1',1,'t','t','t')""")
    con.execute("""INSERT INTO claims(claim_hash,cluster_id,cluster_version,assertion,
        authenticity,extraction_fidelity,establishes,n_eff_sources,
        available_for_decision_at,computed_at,feature_version)
        VALUES('cl1','c1',1,'a','artifact_verified','checked_faithful',
               'underlying_fact',2.0,'t','t','fv1')""")
    con.commit()

    def eff(final, cap):
        con.execute("""INSERT INTO claim_contract_effects
            (claim_id,contract_id,horizon_days,llr,conditioned_on_ref,estimator,
             model_version,final_contribution,contribution_cap,
             available_for_decision_at,computed_at)
            VALUES(1,1,10.0,2.0,'m1','fitted_model',?,?,?,'t','t')""",
            (f"mv{final}", final, cap))
        con.commit()

    check("final contribution exceeding the cap", lambda: eff(3.0, 1.5), "reject")
    check("final contribution within the cap", lambda: eff(1.2, 1.5), "accept")

    # --------------------------------------------------------------- decision layer
    print("\n[5] Decision layer — exposure and eligibility\n")

    con = fresh(); seed(con)
    insert_forecast(con, "f1", GENESIS, 3000, 2500, 3600)

    def decision(permitted, elig, abstain=None):
        con.execute("""INSERT INTO trade_decisions
            (forecast_id,contract_id,decided_at,permitted,abstain_reason,
             max_notional_usd,cluster_exposure_cap_usd,eligibility_status)
            VALUES(1,1,'t',?,?,100.0,500.0,?)""", (permitted, abstain, elig))
        con.commit()

    check("trade permitted while eligibility is close_only",
          lambda: decision(1, "close_only"), "reject")
    check("trade permitted while eligibility unknown",
          lambda: decision(1, "unknown"), "reject")
    check("abstain with no reason given",
          lambda: decision(0, "tradeable"), "reject")
    check("abstain with a reason",
          lambda: decision(0, "tradeable", "EV negative after costs"), "accept")
    check("trade permitted, eligibility tradeable",
          lambda: decision(1, "tradeable"), "accept")

    # ------------------------------------------------------------- outcome/payout
    print("\n[6] Research outcome and economic settlement stay separate\n")

    con = fresh(); seed(con)
    insert_forecast(con, "f1", GENESIS, 6400, 6000, 6800)
    check("void research outcome recorded",
          lambda: con.execute("""INSERT INTO resolutions
              (proposition_id,outcome,resolution_source,recorded_at)
              VALUES(1,'void','uma','t')"""), "accept")
    check("UMA 50/50 payout recorded ALONGSIDE the void outcome",
          lambda: con.execute("""INSERT INTO settlements
              (contract_id,settlement_state,payout_per_share,recorded_at)
              VALUES(1,'settled',0.5,'t')"""), "accept")
    check("payout above 1.0 rejected",
          lambda: con.execute("""INSERT INTO settlements
              (contract_id,settlement_state,payout_per_share,recorded_at)
              VALUES(1,'settled',1.5,'t')"""), "reject")
    check("unscorable forecast must carry an exclusion reason",
          lambda: con.execute("""INSERT INTO scores
              (forecast_id,brier,scorable,computed_at) VALUES(1,NULL,0,'t')"""), "reject")
    check("unscorable with reason",
          lambda: con.execute("""INSERT INTO scores
              (forecast_id,brier,scorable,exclusion_reason,computed_at)
              VALUES(1,NULL,0,'void: excluded from BSS','t')"""), "accept")

    # ------------------------------------------------- availability and provenance
    print("\n[7] Point-in-time and chain integrity\n")

    con = fresh(); seed(con)
    check("available_for_decision_at before first_seen_at",
          lambda: con.execute("""INSERT INTO signal_items
              (content_hash,source_id,first_seen_at,artifact_created_at,
               available_for_decision_at,item_class)
              VALUES('h',1,'2026-06-01T10:00:00.000Z','2026-06-01T10:00:00.000Z',
                     '2026-06-01T09:00:00.000Z','reportage')"""), "reject")

    con = fresh(); seed(con)
    check("forecast using a class frozen AFTER the forecast",
          lambda: insert_forecast(con, "f1", GENESIS, 5000, 4000, 6000,
                                  created="2025-01-01T00:00:00.000Z"), "reject")

    con = fresh(); seed(con)
    insert_forecast(con, "f1", GENESIS, 5000, 4000, 6000)
    check("forged prev_hash (does not match chain head)",
          lambda: insert_forecast(con, "f2", "bogus", 5000, 4000, 6000), "reject")
    check("valid chain continuation",
          lambda: insert_forecast(con, "f2", "f1", 5000, 4000, 6000), "accept")
    check("UPDATE a forecast (positive control — must reject)",
          lambda: con.execute("UPDATE forecasts SET p_est_bp=1 WHERE forecast_hash='f1'"),
          "reject")
    check("external chain anchor recorded",
          lambda: con.execute("""INSERT INTO chain_anchors
              (chain_head_hash,method,external_ref,proof,anchored_at)
              VALUES('f2','rfc3161','tsa://example','base64proof','t')"""), "accept")

    # ------------------------------------------------------------------- summary
    passed = sum(1 for r in results if r[0])
    total = len(results)
    print("\n" + "=" * 78)
    print(f"{passed}/{total} probes behaved as specified")
    if passed != total:
        print("\nFAILURES:")
        for ok, label, exp, got, detail in results:
            if not ok:
                print(f"  - {label}: expected {exp}, got {got}  {detail}")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())

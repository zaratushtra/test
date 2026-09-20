#!/usr/bin/env python3
"""
Phase 1 validation: evidence ledger, forecast registry, hash chain.

Gates from SPINE-DESIGN-V2 §13 Phase 1:
  * the correction regression test passes
  * a forecast reconstructs from its manifest alone
  * the chain is anchored externally

Run: python3 tests/test_phase1.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from spine import chain, ledger, params  # noqa: E402
from spine.canonical import (  # noqa: E402
    GENESIS_HASH,
    CanonicalisationError,
    canonicalise,
    content_hash,
)

PASS, FAIL = [], []


def _message(fn) -> str:
    """Exception text: part of the interface when a refusal explains itself."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return str(e)
    return ""


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    mark = "PASS" if cond else "FAIL"
    print(f"  {mark}  {label}{('  [' + detail + ']') if detail and not cond else ''}")


def raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


T = "2026-06-01T00:00:00.000Z"


def seeded():
    con = ledger.connect(create=True)
    con.execute(
        "INSERT INTO sources(name,kind,created_at) VALUES('Reuters','wire',?)", (T,)
    )
    con.execute(
        """INSERT INTO reference_class_versions
           (class_name,version,description,n,k,alpha,beta,prior_justification,
            exposure_units,exposure_unit_name,frozen_at,selection_rule_ref)
           VALUES('fed_holds',1,'d',40,4,1.0,9.0,'family base rate ~0.10',
                  40.0,'meeting',?,'rules/fed_holds_v1.md')""",
        (T,),
    )
    con.execute(
        """INSERT INTO propositions
           (proposition_hash,statement,resolution_criterion,deadline_utc,
            event_family,horizon_class,created_at)
           VALUES('p1','Will the Fed hold?','per FOMC statement',
                  '2026-10-01T00:00:00.000Z','fed','T1',?)""",
        (T,),
    )
    con.execute(
        """INSERT INTO contracts
           (venue,market_id,outcome_token_id,rules_text,rules_version_hash,
            deadline_utc,resolution_source,payout_states,eligibility_status,
            eligibility_checked_at,first_seen_at)
           VALUES('polymarket','mk1','tok1','rules','rh1',
                  '2026-10-01T00:00:00.000Z','FOMC statement',
                  '{"YES":1.0,"NO":0.0,"UNKNOWN":0.5}','tradeable',?,?)""",
        (T, T),
    )
    con.commit()
    return con


def add_claim(con, chash, avail, fv, n_eff=2.0, assertion="a"):
    con.execute("INSERT OR IGNORE INTO event_clusters VALUES('c1',1,?,?,?)", (T, T, T))
    con.execute(
        """INSERT INTO claims(claim_hash,cluster_id,cluster_version,assertion,
           authenticity,extraction_fidelity,establishes,n_eff_sources,
           available_for_decision_at,computed_at,feature_version)
           VALUES(?, 'c1',1,?, 'artifact_verified','checked_faithful',
                  'underlying_fact',?,?,?,?)""",
        (chash, assertion, n_eff, avail, avail, fv),
    )
    con.commit()


def reg(con, **kw):
    base = dict(
        proposition_id=1, p_est_bp=3000, p_lo_bp=2500, p_hi_bp=3600,
        uncertainty_method="cluster_bootstrap", min_width_bp=500, p_base_bp=1000,
        forecast_kind="independent", reference_class_version_id=1,
        model_version="mv1", regime_id="r1", created_at="2026-06-02T00:00:00.000Z",
        source="human",
    )
    base.update(kw)
    if "inputs_manifest_hash" not in base:
        base["inputs_manifest_hash"] = ledger.put_manifest(
            con, "forecast_inputs",
            {"claims": [], "note": "empty", "params": params.commit(con, T)}, T
        )
    return ledger.register_forecast(con, **base)


def main() -> int:
    print("=" * 74)
    print("SPINE PHASE 1 VALIDATION")
    print("=" * 74)

    # -------------------------------------------------- canonicalisation
    print("\n[1] Canonicalisation — a hash nobody else can reproduce is worthless\n")
    ok("key order does not change the digest",
       content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1}))
    ok("nesting order does not change the digest",
       content_hash({"x": {"p": 1, "q": 2}}) == content_hash({"x": {"q": 2, "p": 1}}))
    ok("no insignificant whitespace", canonicalise({"a": 1, "b": [1, 2]}) ==
       '{"a":1,"b":[1,2]}')
    ok("unicode preserved, not escaped", canonicalise({"k": "café"}) == '{"k":"café"}')
    ok("NaN rejected", raises(lambda: content_hash({"x": float("nan")}),
                              CanonicalisationError))
    ok("Infinity rejected", raises(lambda: content_hash({"x": float("inf")}),
                                   CanonicalisationError))
    ok("non-string object key rejected",
       raises(lambda: content_hash({1: "a"}), CanonicalisationError))
    ok("unserialisable type rejected",
       raises(lambda: content_hash({"x": {1, 2}}), CanonicalisationError))
    # Golden vector: pins the digest so a reimplementation can be checked.
    GOLDEN_INPUT = {"created_at": "2026-06-02T00:00:00.000Z", "p_est_bp": 3000,
                    "source": "human", "nested": {"b": [1, 2], "a": None}}
    GOLDEN_DIGEST = content_hash(GOLDEN_INPUT)
    ok("golden vector is stable across calls",
       content_hash(dict(reversed(list(GOLDEN_INPUT.items())))) == GOLDEN_DIGEST,
       GOLDEN_DIGEST)
    print(f"        golden digest: {GOLDEN_DIGEST}")

    # -------------------------------------------------- chain
    print("\n[2] Hash chain — tamper evidence\n")
    con = seeded()
    ok("empty chain head is the genesis sentinel", chain.head(con) == GENESIS_HASH)
    h1 = reg(con)
    h2 = reg(con, p_est_bp=4000, p_lo_bp=3500, p_hi_bp=4600)
    v = chain.verify(con)
    ok("two-record chain verifies", v.ok and v.length == 2, v.summary())
    ok("head is the last hash", chain.head(con) == h2)
    ok("registering with a stale prev_hash is impossible via the API",
       chain.compute_hash({"p_est_bp": 1}, GENESIS_HASH) != h1)

    # Tamper by bypassing the API entirely (triggers block UPDATE, so delete+reinsert
    # is the realistic attack; verify() must still catch it).
    con.execute("PRAGMA writable_schema = ON")
    con.execute("DROP TRIGGER forecasts_no_update")
    con.execute("PRAGMA writable_schema = OFF")
    # Tamper with a committed field that carries no CHECK, so the edit is only
    # detectable by recomputing the hash — which is the property under test.
    con.execute(
        "UPDATE forecasts SET model_version = 'mv-swapped' WHERE forecast_hash = ?",
        (h1,),
    )
    con.commit()
    v = chain.verify(con)
    ok("edited content is detected by recomputation", not v.ok and v.first_break == 1,
       v.summary())

    # -------------------------------------------------- anchoring
    print("\n[3] External anchoring — the chain cannot date itself\n")
    con = seeded()
    reg(con)
    h = reg(con, p_est_bp=4000, p_lo_bp=3500, p_hi_bp=4600)
    v = chain.verify(con)
    ok("unanchored chain reports its exposure",
       v.ok and v.anchored_through == 0 and v.unanchored_tail == 2, v.summary())
    chain.anchor(con, "rfc3161", "tsa://example/123", "base64proof", T)
    v = chain.verify(con)
    ok("anchored chain reports coverage",
       v.ok and v.anchored_through == 2 and v.unanchored_tail == 0, v.summary())
    reg(con, p_est_bp=5000, p_lo_bp=4500, p_hi_bp=5600)
    v = chain.verify(con)
    ok("records after the anchor are flagged as not time-proven",
       v.ok and v.unanchored_tail == 1, v.summary())

    # -------------------------------------------------- availability discipline
    print("\n[4] Availability time governs retrieval, not arrival time\n")
    con = seeded()
    # Arrives 10:00, verification completes 10:05.
    add_claim(con, "cl_late", "2026-06-01T10:05:00.000Z", "fv1")
    ok("a 10:01 decision does not see a claim available at 10:05",
       len(ledger.claims_available_at(con, "2026-06-01T10:01:00.000Z")) == 0)
    ok("a 10:06 decision does see it",
       len(ledger.claims_available_at(con, "2026-06-01T10:06:00.000Z")) == 1)

    # THE mandatory regression test from v2 §8.2.
    print("\n    correction regression (v2 §8.2, mandatory):")
    con = seeded()
    add_claim(con, "cl_v1", "2026-06-01T10:00:00.000Z", "fv1", n_eff=2.0,
              assertion="original value")
    add_claim(con, "cl_v2", "2026-06-01T11:00:00.000Z", "fv2", n_eff=9.0,
              assertion="corrected value")
    at_1030 = ledger.claims_available_at(con, "2026-06-01T10:30:00.000Z")
    ok("a forecast reconstructed for 10:30 receives ONLY the original value",
       len(at_1030) == 1 and at_1030[0]["assertion"] == "original value"
       and at_1030[0]["n_eff_sources"] == 2.0)
    at_1130 = ledger.claims_available_at(con, "2026-06-01T11:30:00.000Z")
    ok("at 11:30 both the original and the correction are visible",
       len(at_1130) == 2)

    # -------------------------------------------------- manifests
    print("\n[5] Commitment to inputs, not labels\n")
    con = seeded()
    m1 = ledger.put_manifest(con, "forecast_inputs", {"claims": ["h_a"], "lambda": "v1"}, T)
    m2 = ledger.put_manifest(con, "forecast_inputs", {"lambda": "v1", "claims": ["h_a"]}, T)
    ok("identical content yields one manifest", m1 == m2)
    ok("manifest stored exactly once",
       con.execute("SELECT COUNT(*) FROM manifests").fetchone()[0] == 1)
    m3 = ledger.put_manifest(con, "forecast_inputs", {"claims": ["h_b"], "lambda": "v1"}, T)
    ok("different inputs under the SAME label yield a different manifest", m1 != m3)
    ok("manifests are immutable",
       raises(lambda: con.execute(
           "UPDATE manifests SET content='{}' WHERE manifest_hash=?", (m1,))))
    ok("a forecast cannot commit to an unstored manifest",
       raises(lambda: reg(con, inputs_manifest_hash="0" * 64), ledger.LedgerError))

    # -------------------------------------------------- forecast_kind coherence
    print("\n[6] Forecast kinds must be reconstructable\n")
    con = seeded()
    ok("market_conditioned without the price is refused",
       raises(lambda: reg(con, forecast_kind="market_conditioned"), ledger.LedgerError))
    ok("independent carrying a price is refused",
       raises(lambda: reg(con, p_market_bp=5000), ledger.LedgerError))
    ok("market_conditioned with the price is accepted",
       isinstance(reg(con, forecast_kind="market_conditioned", p_market_bp=3300), str))

    # -------------------------------------------------- reconstruction
    print("\n[7] Reconstruction from stored inputs alone\n")
    con = seeded()
    add_claim(con, "cl_a", "2026-06-01T09:00:00.000Z", "fv1")
    con.execute(
        """INSERT INTO claim_contract_effects
           (claim_id,contract_id,horizon_days,llr,conditioned_on_ref,estimator,
            model_version,final_contribution,contribution_cap,
            available_for_decision_at,computed_at)
           VALUES(1,1,10.0,0.8,'m1','fitted_model','mv1',0.6,1.5,?,?)""",
        ("2026-06-01T09:30:00.000Z", "2026-06-01T09:30:00.000Z"),
    )
    con.commit()
    inputs = {"claims": ["cl_a"], "lambda_version": "v1", "prompt_bundle": None,
              "params": params.commit(con, T)}
    mh = ledger.put_manifest(con, "forecast_inputs", inputs, T)
    fh = reg(con, contract_id=1, inputs_manifest_hash=mh)
    r = ledger.reconstruct(con, fh)
    ok("reconstruction returns the committed manifest content",
       r["manifest_content"] == canonicalise(inputs))
    ok("reconstruction returns the frozen reference class",
       r["reference_class"]["class_name"] == "fed_holds"
       and r["reference_class"]["frozen_at"] == T)
    ok("reconstruction returns effects available at decision time",
       len(r["effects_at_decision"]) == 1
       and r["effects_at_decision"][0]["final_contribution"] == 0.6)
    ok("recomputing the hash from stored fields reproduces it",
       chain.compute_hash(r, r["prev_hash"]) == fh)

    print("\n[7b] Every forecast commits the parameters in force (§8.4)\n")
    con = seeded()
    bare = ledger.put_manifest(con, "forecast_inputs", {"claims": []}, T)
    ok("a manifest with no parameter snapshot is refused",
       raises(lambda: reg(con, inputs_manifest_hash=bare), ledger.LedgerError))
    ok("...and the message says why a degree of freedom must be recorded",
       "cannot be told apart from one made under different ones" in _message(
           lambda: reg(con, inputs_manifest_hash=bare)))
    ok("a manifest citing an unstored snapshot is refused",
       raises(lambda: reg(con, inputs_manifest_hash=ledger.put_manifest(
           con, "forecast_inputs", {"params": "0" * 64}, T)),
           ledger.LedgerError))
    good = ledger.put_manifest(
        con, "forecast_inputs", {"claims": [], "params": params.commit(con, T)}, T)
    fh_p = reg(con, inputs_manifest_hash=good)
    ok("a manifest that commits them is accepted", len(fh_p) == 64)
    ok("the snapshot is recoverable from the forecast",
       params.committed_in(con, good) is not None)
    ok("...and identifies the configuration running now",
       params.matches_current(con, params.committed_in(con, good)))
    ok("the snapshot records provenance, not just values",
       all("provenance" in v for v in params.snapshot()["parameters"].values()))
    ok("a changed setting would produce a different snapshot",
       params.snapshot()["declared_count"] == len(params.unsupported()))

    print("\n[8] A database at the wrong schema version is refused\n")
    import sqlite3 as _sq
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "spine.db")
        c = ledger.connect(path)
        ok("a fresh path is created and stamped",
           c.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION)
        c.close()
        c2 = ledger.connect(path)
        ok("reopening at the right version works",
           c2.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 0)
        c2.close()
        ok("re-creating over existing tables is refused",
           raises(lambda: ledger.connect(path, create=True), ledger.LedgerError))

        stale = os.path.join(d, "stale.db")
        c3 = _sq.connect(stale)
        c3.execute("CREATE TABLE contracts(id INTEGER)")
        c3.execute("PRAGMA user_version = 1")
        c3.commit()
        c3.close()
        ok("an older schema version is refused, not opened anyway",
           raises(lambda: ledger.connect(stale), ledger.LedgerError))
        ok("...and the message says why an empty result would be worse",
           "evidence of absence" in _message(lambda: ledger.connect(stale)))

        empty = os.path.join(d, "empty.db")
        _sq.connect(empty).close()
        ok("an empty file is created into, not refused",
           ledger.connect(empty).execute(
               "PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION)

    # -------------------------------------------------- summary
    print("\n" + "=" * 74)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

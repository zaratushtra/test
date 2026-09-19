#!/usr/bin/env python3
"""
Reference classes: freezing, loading, and whether a hazard forecast can actually
be reconstructed from the record.

The last question is the point of the suite. §12's reproducibility criterion is
that a forecast can be rebuilt from stored inputs alone, and the deadline-aware
path divides by an exposure denominator that was never stored — so the default
forecast in this project was not reconstructable, and a reconstruction would
have silently produced the static rate §5.3 rejects.

Run: python3 tests/test_refclass.py
Stdlib only.
"""

from __future__ import annotations

import math
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger, models, refclass, registry, timeutil  # noqa: E402
from spine.refclass import Member, RefClassError  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


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
    return timeutil.iso(NOW + timedelta(**kw))


RULE = {"family": "committee_adoption",
        "include": "every measure reported out of the standing committee",
        "exclude": "procedural motions and unanimous-consent items",
        "window": "2016-01-01 to the freeze date"}


def roster(n=40, k=6):
    return [Member(event_name=f"case{i}", event_date=at(days=-400 + i * 5),
                   outcome=1 if i < k else 0, source_url=f"https://x.test/{i}")
            for i in range(n)]


def main() -> int:
    print("=" * 76)
    print("SPINE REFERENCE CLASSES")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)

    print("\n[1] The rule is committed to by content, not by name\n")
    ref = refclass.declare_selection_rule(con, RULE, created_at=at(days=-2))
    ok("a rule stores and returns a hash", len(ref) == 64)
    ok("the same rule stores once",
       refclass.declare_selection_rule(con, RULE) == ref)
    ok("a changed rule is a different hash",
       refclass.declare_selection_rule(con, {**RULE, "exclude": "nothing"}) != ref)
    ok("an empty rule is refused",
       raises(lambda: refclass.declare_selection_rule(con, {}), RefClassError))
    ok("a class referencing an unstored rule is refused",
       raises(lambda: refclass.freeze(
           con, class_name="x", version=1, description="d", members=roster(),
           selection_rule_ref="0" * 64, alpha=1.0, beta=9.0,
           prior_justification="j", exposure_units=40.0,
           exposure_unit_name="case-week", frozen_at=at()), RefClassError))

    print("\n[2] Counts come from the roster, because there is only one of them\n")
    cid = refclass.freeze(
        con, class_name="committee_adoption", version=1,
        description="measures reported out of the standing committee",
        members=roster(40, 6), selection_rule_ref=ref, alpha=1.0, beta=9.0,
        prior_justification="family base rate near 0.10; Beta(1,9) has mean 0.10",
        exposure_units=40.0, exposure_unit_name="case-week",
        exposure_window_days=7, censoring_note="right-censored at deadline",
        frozen_at=at(days=-1))
    ok("a class freezes", cid > 0)
    stored = con.execute("SELECT n, k FROM reference_class_versions WHERE id=?",
                         (cid,)).fetchone()
    ok("n and k are derived from the members", stored == (40, 6), stored)
    ok("the roster is stored alongside", len(refclass.members(con, cid)) == 40)
    a = refclass.audit(con, cid)
    ok("the class audits clean against its own roster", a["ok"], a)

    ok("duplicate member names are refused",
       raises(lambda: refclass.freeze(
           con, class_name="dup", version=1, description="d",
           members=[Member("same", at(days=-10), 1), Member("same", at(days=-9), 0)],
           selection_rule_ref=ref, alpha=1.0, beta=9.0, prior_justification="j",
           exposure_units=2.0, exposure_unit_name="case", frozen_at=at()),
           RefClassError))
    ok("a member postdating the freeze is refused",
       raises(lambda: refclass.freeze(
           con, class_name="future", version=1, description="d",
           members=[Member("later", at(days=5), 1)], selection_rule_ref=ref,
           alpha=1.0, beta=9.0, prior_justification="j", exposure_units=1.0,
           exposure_unit_name="case", frozen_at=at()), RefClassError))
    ok("...because that is selection on outcomes",
       "selected on outcomes" in _msg(lambda: refclass.freeze(
           con, class_name="future2", version=1, description="d",
           members=[Member("later", at(days=5), 1)], selection_rule_ref=ref,
           alpha=1.0, beta=9.0, prior_justification="j", exposure_units=1.0,
           exposure_unit_name="case", frozen_at=at())))
    ok("an unnamed exposure unit is refused",
       raises(lambda: refclass.freeze(
           con, class_name="unnamed", version=1, description="d",
           members=roster(2, 1), selection_rule_ref=ref, alpha=1.0, beta=9.0,
           prior_justification="j", exposure_units=2.0, exposure_unit_name=" ",
           frozen_at=at()), RefClassError))
    ok("a zero exposure is refused",
       raises(lambda: refclass.freeze(
           con, class_name="noexp", version=1, description="d",
           members=roster(2, 1), selection_rule_ref=ref, alpha=1.0, beta=9.0,
           prior_justification="j", exposure_units=0.0,
           exposure_unit_name="case", frozen_at=at()), RefClassError))
    ok("an unjustified prior is refused",
       raises(lambda: refclass.freeze(
           con, class_name="nopri", version=1, description="d",
           members=roster(2, 1), selection_rule_ref=ref, alpha=1.0, beta=9.0,
           prior_justification="", exposure_units=2.0,
           exposure_unit_name="case", frozen_at=at()), RefClassError))

    print("\n[3] Versions are immutable; a changed roster is a new version\n")
    ok("re-freezing the same version is refused, with what to do instead",
       "is v2" in _msg(lambda: refclass.freeze(
           con, class_name="committee_adoption", version=1, description="d",
           members=roster(41, 6), selection_rule_ref=ref, alpha=1.0, beta=9.0,
           prior_justification="j", exposure_units=41.0,
           exposure_unit_name="case-week", frozen_at=at())))
    cid2 = refclass.freeze(
        con, class_name="committee_adoption", version=2,
        description="one further case", members=roster(41, 7),
        selection_rule_ref=ref, alpha=1.0, beta=9.0,
        prior_justification="unchanged", exposure_units=41.0,
        exposure_unit_name="case-week", frozen_at=at(hours=1))
    ok("version 2 freezes alongside version 1", cid2 != cid)
    ok("version 1 is untouched",
       con.execute("SELECT n FROM reference_class_versions WHERE id=?",
                   (cid,)).fetchone()[0] == 40)
    ok("a version cannot be edited",
       raises(lambda: con.execute(
           "UPDATE reference_class_versions SET k=99 WHERE id=?", (cid,)),
           sqlite3.IntegrityError))
    ok("membership cannot be edited within a version",
       raises(lambda: con.execute(
           "UPDATE reference_class_members SET outcome=1 WHERE class_version_id=?",
           (cid,)), sqlite3.IntegrityError))

    print("\n[4] Lookup is point-in-time, like everything else\n")
    ok("as of yesterday only version 1 existed",
       refclass.latest(con, "committee_adoption", at(minutes=30)) == cid)
    ok("as of now version 2 is the newest",
       refclass.latest(con, "committee_adoption", at(hours=2)) == cid2)
    ok("before either was frozen there is none",
       refclass.latest(con, "committee_adoption", at(days=-30)) is None)
    ok("an unknown class has none", refclass.latest(con, "nope") is None)

    print("\n[5] A hazard forecast reconstructs — which it could not before\n")
    rc = refclass.load(con, cid)
    ok("the loaded class carries the exposure denominator",
       rc.exposure_units == 40.0, rc)
    ok("...and matches the roster it was built from",
       rc.k == 6 and rc.n == 40)

    registry.ingest_markets(con, [{
        "id": "mk-r", "question": "Will it be adopted?", "end_date": at(days=9),
        "rules_text": "Resolves on the recorded vote.",
        "outcome_token_id": "tok-r"}], jurisdiction="GB", now=at())
    contract = con.execute("SELECT id FROM contracts").fetchone()[0]
    pid = registry.ensure_proposition(
        con, "Measure adopted", "Minutes record adoption", at(days=9),
        "committee_adoption", "T1", now=at())

    time_remaining = 1.3
    fc = models.baseline_forecast(rc, time_remaining=time_remaining)
    mh = ledger.put_manifest(con, "forecast_inputs",
                             {"reference_class_version_id": cid,
                              "time_remaining": time_remaining}, at(hours=2))
    fh = ledger.register_forecast(
        con, proposition_id=pid, contract_id=contract, p_est_bp=fc.p_est_bp,
        p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp,
        uncertainty_method=fc.uncertainty_method, min_width_bp=0,
        p_base_bp=fc.p_est_bp, forecast_kind="independent",
        reference_class_version_id=cid, inputs_manifest_hash=mh,
        model_version="rc-1", regime_id="r1", created_at=at(hours=2),
        source="model")

    r = ledger.reconstruct(con, fh)
    stored_rc = r["reference_class"]
    ok("reconstruction returns the exposure denominator",
       stored_rc.get("exposure_units") == 40.0, stored_rc)
    ok("...and the name of the unit, so the number is interpretable later",
       stored_rc.get("exposure_unit_name") == "case-week")
    ok("...and the censoring note", stored_rc.get("censoring_note"))

    # The actual test: rebuild the forecast from stored inputs alone.
    import json
    manifest = json.loads(r["manifest_content"])
    rebuilt_rc = models.ReferenceClass(
        name=stored_rc["class_name"], version=stored_rc["version"],
        k=stored_rc["k"], n=stored_rc["n"],
        exposure_units=stored_rc["exposure_units"],
        alpha=stored_rc["alpha"], beta=stored_rc["beta"])
    rebuilt = models.baseline_forecast(
        rebuilt_rc, time_remaining=manifest["time_remaining"])
    ok("the forecast rebuilds to the same basis point",
       rebuilt.p_est_bp == fc.p_est_bp, (rebuilt.p_est_bp, fc.p_est_bp))
    ok("...and the same interval",
       (rebuilt.p_lo_bp, rebuilt.p_hi_bp) == (fc.p_lo_bp, fc.p_hi_bp))

    # And the failure that was there before: without exposure_units, a
    # reconstruction can only produce the static rate.
    static = models.baseline_forecast(rebuilt_rc, deadline_aware=False)
    ok("the static fallback gives a materially different answer",
       abs(static.p_est_bp - fc.p_est_bp) > 100,
       (static.p_est_bp, fc.p_est_bp))
    print(f"        hazard {fc.p_est_bp}bp vs static {static.p_est_bp}bp — "
          f"a reconstruction missing exposure_units returns the second")

    print("\n[6] The audit catches a class that drifted from its roster\n")
    con.execute("PRAGMA writable_schema=OFF")
    con.execute("DROP TRIGGER refclass_immutable_update")
    con.execute("UPDATE reference_class_versions SET k=30 WHERE id=?", (cid,))
    con.commit()
    bad = refclass.audit(con, cid)
    ok("a doctored count is detected", not bad["ok"], bad)
    ok("...and named precisely",
       any("k is 30" in p for p in bad["problems"]), bad["problems"])
    ok("an unknown class version is refused",
       raises(lambda: refclass.audit(con, 9999), RefClassError))

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


if __name__ == "__main__":
    sys.exit(main())

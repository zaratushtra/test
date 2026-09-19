#!/usr/bin/env python3
"""
Evaluation validation: scoring the record, exclusions, settlement divergence,
the dependence DAG, and the readout that refuses to fire below twelve regimes.

Run: python3 tests/test_evaluate.py
Stdlib only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import evaluate, ledger, models, registry, timeutil  # noqa: E402
from spine.evaluate import EvaluationError  # noqa: E402
from spine.models import ReferenceClass  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)
RC = ReferenceClass("committee_adoption", 1, k=6, n=40, exposure_units=40.0,
                    alpha=1.0, beta=9.0)


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


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


def at(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def build(con, n_regimes=12, per=10, seed=5):
    """A record: contracts, propositions, forecasts, resolutions."""
    import random
    from spine import ledger as L
    rng = random.Random(seed)
    registry.ingest_markets(con, [{
        "id": "mk-e", "question": "Will it pass?", "end_date": at(days=20),
        "rules_text": "Resolves on the recorded vote.",
        "outcome_token_id": "tok-e"}], jurisdiction="GB", now=at())
    cid = con.execute("SELECT id FROM contracts").fetchone()[0]
    rcid = con.execute(
        """INSERT INTO reference_class_versions
           (class_name, version, description, n, k, alpha, beta,
            prior_justification, exposure_window_days, censoring_note,
            frozen_at, selection_rule_ref)
           VALUES ('committee_adoption',1,'d',40,6,1.0,9.0,'p',7,'c',?,'r')""",
        (at(days=-1),)).lastrowid
    con.commit()

    made = []
    for g in range(n_regimes):
        for i in range(per):
            created = NOW + timedelta(hours=g * 24 + i)
            truth = rng.uniform(0.05, 0.95)
            pid = registry.ensure_proposition(
                con, f"P {g}_{i} passes", "Recorded vote",
                timeutil.iso(created + timedelta(days=9)),
                "committee_adoption", "T1",
                now=timeutil.iso(created))
            base = models.baseline_forecast(RC, time_remaining=1.0)
            shift = models.logit(truth) - models.logit(base.p_est_bp / 10000)
            fc = models.independent_forecast(base, [shift * 0.9])
            mh = L.put_manifest(con, "forecast_inputs", {"q": f"{g}_{i}"},
                                timeutil.iso(created))
            fh = L.register_forecast(
                con, proposition_id=pid, contract_id=cid, p_est_bp=fc.p_est_bp,
                p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp,
                uncertainty_method=fc.uncertainty_method, min_width_bp=0,
                p_base_bp=base.p_est_bp, forecast_kind="independent",
                reference_class_version_id=rcid, inputs_manifest_hash=mh,
                model_version="ev-1", regime_id=f"regime{g}",
                created_at=timeutil.iso(created),
                label_available_at=timeutil.iso(created + timedelta(days=10)),
                source="model")
            made.append((pid, fh, 1 if rng.random() < truth else 0))
    return cid, made


def main() -> int:
    print("=" * 76)
    print("SPINE EVALUATION VALIDATION")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)
    cid, made = build(con)

    print("\n[1] Scoring is point-in-time in two senses at once\n")
    # as_of gates BOTH which forecasts exist yet and whether their labels were
    # knowable. The first is easy to overlook and worth pinning: a forecast made
    # after the scoring moment is not part of that record.
    early = evaluate.score_all(con, as_of=at(hours=1))
    ok("a forecast created after the scoring time is not in that record",
       early.total == 2, early.summary())
    ok("...and the two that existed are excluded as unresolved",
       early.excluded.get("unresolved") == 2, early.summary())

    run0 = evaluate.score_all(con, as_of=at(days=20))
    ok("the rest of the record scores as unresolved while no outcomes exist",
       run0.scored == 0 and sum(run0.excluded.values()) == 118, run0.summary())
    ok("the exclusion rate is 100% and says so", run0.exclusion_rate == 1.0)
    ok("the two rows from the earlier pass are not recomputed",
       run0.already_scored == 2, run0.already_scored)
    print(f"        {run0.summary()}")

    for pid, _, outcome in made:
        con.execute(
            "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
            "resolved_at, recorded_at) VALUES(?,?,?,?,?)",
            (pid, "resolved_yes" if outcome else "resolved_no", "minutes",
             at(days=40), at(days=40)))
    con.commit()

    run1 = evaluate.score_all(con, as_of=at(days=40))
    ok("a resolution arriving later does NOT rewrite the existing exclusions",
       run1.scored == 0 and run1.already_scored == 120, run1.summary())
    ok("...so the record still shows 120 unscorable rows",
       con.execute("SELECT COUNT(*) FROM scores WHERE scorable=0").fetchone()[0]
       == 120)
    print(f"        a score that silently changed would undo the chain: "
          f"{run1.summary()}")

    print("\n[2] Labels gate scoring the way availability gates retrieval\n")
    con2 = ledger.connect(":memory:", create=True)
    cid2, made2 = build(con2)
    for pid, _, outcome in made2:
        con2.execute(
            "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
            "resolved_at, recorded_at) VALUES(?,?,?,?,?)",
            (pid, "resolved_yes" if outcome else "resolved_no", "minutes",
             at(days=40), at(days=40)))
    con2.commit()
    # Every forecast exists and is resolved, but no label was knowable yet.
    mid = evaluate.score_all(con2, as_of=at(days=12))
    ok("resolved but not yet knowable is excluded on that ground",
       mid.excluded.get("label_not_available", 0) > 0, mid.summary())
    ok("...while the forecasts whose labels HAVE landed are scored",
       mid.scored > 0 and mid.scored < mid.total, mid.summary())
    ok("the two populations partition the window exactly",
       mid.scored + mid.excluded["label_not_available"] == mid.total,
       mid.summary())
    ok("...and every forecast in the window is accounted for",
       mid.total == con2.execute(
           "SELECT COUNT(*) FROM forecasts WHERE created_at <= ?",
           (at(days=12),)).fetchone()[0])
    print(f"        {mid.summary()}")

    con3 = ledger.connect(":memory:", create=True)
    cid3, made3 = build(con3)
    for i, (pid, _, outcome) in enumerate(made3):
        # A few unscorable outcomes, which must be counted rather than dropped.
        out = ("void" if i % 40 == 0 else
               "disputed" if i % 41 == 0 else
               ("resolved_yes" if outcome else "resolved_no"))
        con3.execute(
            "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
            "resolved_at, recorded_at) VALUES(?,?,?,?,?)",
            (pid, out, "minutes", at(days=40), at(days=40)))
    con3.commit()
    run3 = evaluate.score_all(con3, as_of=at(days=40))
    ok("void and disputed outcomes are excluded with distinct reasons",
       "void" in run3.excluded and "disputed" in run3.excluded, run3.excluded)
    ok("the exclusion rate is reported next to the score",
       0 < run3.exclusion_rate < 0.1, run3.exclusion_rate)
    ok("every forecast has exactly one score row",
       con3.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 120)
    ok("an excluded row carries a human-readable reason",
       "voided" in con3.execute(
           "SELECT exclusion_reason FROM scores WHERE exclusion_reason LIKE "
           "'%void%' LIMIT 1").fetchone()[0])
    print(f"        {run3.summary()}")

    print("\n[3] Observations come from the record, not from a list\n")
    obs = evaluate.observations(con3)
    ok("only scorable forecasts become observations",
       len(obs) == run3.scored, (len(obs), run3.scored))
    ok("observations are keyed by proposition, so they can be paired",
       all(o.key.startswith("prop:") for o in obs))
    ok("filtering to a model version that does not exist yields nothing",
       evaluate.observations(con3, model_version="nope") == [])
    ok("filtering to the real model version yields everything",
       len(evaluate.observations(con3, model_version="ev-1")) == len(obs))
    ok("filtering by event family works",
       len(evaluate.observations(con3, event_family="committee_adoption")) == len(obs))
    sl = evaluate.slices(con3)
    ok("slices show what would be averaged over", len(sl) == 1 and sl[0]["n"] == 120,
       sl)
    print(f"        slices: {sl}")

    print("\n[4] The gate refuses to fire below twelve regimes\n")
    ev = evaluate.evaluate(con3, resamples=800)
    ok("twelve regimes evaluates", ev.blocked_by is None, ev.blocked_by)
    ok("a real edge fires the gate", ev.gate_fires, ev.summary())
    ok("calibration is reported separately from skill",
       len(evaluate.calibration(con3)) > 0)
    print("        " + ev.summary().replace("\n", "\n        "))

    con4 = ledger.connect(":memory:", create=True)
    _, made4 = build(con4, n_regimes=4, per=10, seed=9)
    for pid, _, outcome in made4:
        con4.execute(
            "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
            "resolved_at, recorded_at) VALUES(?,?,?,?,?)",
            (pid, "resolved_yes" if outcome else "resolved_no", "m",
             at(days=40), at(days=40)))
    con4.commit()
    evaluate.score_all(con4, as_of=at(days=40))
    ev4 = evaluate.evaluate(con4, resamples=800)
    ok("four regimes reports descriptive statistics", ev4.brier is not None)
    ok("...and says the gate is NOT EVALUABLE rather than giving an interval",
       ev4.blocked_by is not None and ev4.ci_lower is None, ev4.blocked_by)
    ok("...and does not claim the gate fired", not ev4.gate_fires)
    print("        " + ev4.summary().replace("\n", "\n        "))
    ok("an empty record evaluates to nothing, not to zero",
       evaluate.evaluate(ledger.connect(":memory:", create=True)).n == 0)

    print("\n[4b] A non-canonical timestamp is refused where it is committed\n")
    from spine import ledger as L
    cid5, _ = build(ledger.connect(":memory:", create=True), n_regimes=1, per=1)
    con5 = ledger.connect(":memory:", create=True)
    build(con5, n_regimes=1, per=1)
    mh5 = L.put_manifest(con5, "forecast_inputs", {"q": "tz"}, at(days=1))
    kw = dict(proposition_id=1, contract_id=1, p_est_bp=5000, p_lo_bp=4000,
              p_hi_bp=6000, uncertainty_method="x", min_width_bp=0,
              p_base_bp=5000, forecast_kind="independent",
              reference_class_version_id=1, inputs_manifest_hash=mh5,
              model_version="ev-1", regime_id="r", source="model")
    ok("a whole-second created_at is refused with an explanation, not a CHECK error",
       "invisible to a decision" in _msg(
           lambda: L.register_forecast(con5, created_at="2026-09-21T00:00:00Z", **kw)))
    ok("a non-canonical label_available_at is refused too",
       raises(lambda: L.register_forecast(
           con5, created_at=at(days=2), label_available_at="2026-10-01T00:00:00Z",
           **kw), L.LedgerError))
    ok("the canonical form is accepted",
       len(L.register_forecast(con5, created_at=at(days=2),
                               label_available_at=at(days=12), **kw)) == 64)

    print("\n[5] Settlement is the economic outcome and may disagree\n")
    evaluate.record_settlement(con3, cid3, "settled", 1.0, settled_at=at(days=41))
    ok("a settlement records", con3.execute(
        "SELECT payout_per_share FROM settlements WHERE contract_id=?",
        (cid3,)).fetchone()[0] == 1.0)
    prop_yes = next(p for p, _, o in made3 if o == 1)
    registry.bind(con3, prop_yes, cid3, "exact", "test", now=at())
    ok("a matching settlement shows no divergence",
       evaluate.settlement_divergence(con3) == []
       or con3.execute("SELECT outcome FROM resolutions WHERE proposition_id=?",
                       (prop_yes,)).fetchone()[0] != "resolved_yes")

    evaluate.record_settlement(con3, cid3, "settled", 0.5,
                               note="UMA resolved Unknown")
    div = evaluate.settlement_divergence(con3)
    ok("a UMA 50/50 on a cleanly resolved proposition IS a divergence",
       len(div) >= 1, div)
    ok("...and the binding quality travels with it",
       div and div[0]["match_quality"] == "exact")
    ok("the payout is not forced to agree with the research outcome",
       con3.execute("SELECT payout_per_share FROM settlements WHERE contract_id=?",
                    (cid3,)).fetchone()[0] == 0.5)
    ok("a payout outside [0,1] is refused by the schema",
       raises(lambda: evaluate.record_settlement(con3, cid3, "settled", 1.5),
              sqlite3.IntegrityError))
    print(f"        divergence: {div[0] if div else 'none'}")

    print("\n[6] Score correlation is not loss correlation\n")
    hashes = [h for _, h, _ in made3[:4]]
    evaluate.link(con3, hashes[0], hashes[1], conditional_prob=0.7,
                  dependence_kind="score", correlation=0.6, created_at=at())
    evaluate.link(con3, hashes[1], hashes[2], conditional_prob=0.6,
                  dependence_kind="score", correlation=0.5, created_at=at())
    ok("dependence is transitive along one kind",
       evaluate.dependents(con3, hashes[0], "score") == sorted(hashes[1:3]))
    ok("a different dependence kind is a different graph",
       evaluate.dependents(con3, hashes[0], "loss") == [])
    evaluate.link(con3, hashes[2], hashes[0], conditional_prob=0.5,
                  dependence_kind="loss", correlation=0.9, created_at=at())
    ok("...so the same pair may cycle in one kind and not the other",
       evaluate.dependents(con3, hashes[2], "loss") == [hashes[0]])
    ok("a cycle within one kind is refused",
       raises(lambda: evaluate.link(
           con3, hashes[2], hashes[0], conditional_prob=0.5,
           dependence_kind="score", correlation=0.9), sqlite3.IntegrityError))
    ok("an unknown dependence kind is refused",
       raises(lambda: evaluate.link(con3, hashes[0], hashes[3],
                                    conditional_prob=0.5,
                                    dependence_kind="vibes", correlation=0.1),
              EvaluationError))
    ok("a self-edge is refused",
       raises(lambda: evaluate.link(con3, hashes[0], hashes[0],
                                    conditional_prob=0.5,
                                    dependence_kind="score", correlation=0.1),
              sqlite3.IntegrityError))

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

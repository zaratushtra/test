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
    ok("a resolution arriving later scores the forecast at the new revision",
       run1.scored == 120, run1.summary())
    ok("...without rewriting the earlier exclusion, which stays on the record",
       con.execute("SELECT COUNT(*) FROM scores WHERE scorable=0").fetchone()[0]
       == 120)
    ok("so each forecast now carries two score rows, revision 0 and revision 1",
       con.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 240)
    ok("and the current score is the one against the live resolution",
       con.execute("SELECT COUNT(*) FROM current_scores WHERE scorable=1")
       .fetchone()[0] == 120)
    ok("scores are append-only",
       raises(lambda: con.execute("UPDATE scores SET brier=0.0 WHERE id=1"),
              sqlite3.IntegrityError))
    print(f"        {run1.summary()}")
    print("        a forecast excluded as unresolved is rescored once the "
          "outcome lands, rather than staying excluded forever")

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

    print("\n[4] The gate needs a declared budget before it will fire\n")
    unplanned = evaluate.evaluate(con3, resamples=800)
    ok("with no plan the gate is NOT EVALUABLE, whatever the data says",
       unplanned.blocked_by is not None and "no evaluation plan" in unplanned.blocked_by,
       unplanned.blocked_by)
    ok("...while the descriptive statistics are still reported",
       unplanned.brier is not None and unplanned.bss is not None)
    ok("...and it does not claim the gate fired", not unplanned.gate_fires)

    ok("a plan with no named declarer is refused",
       raises(lambda: evaluate.declare_plan(con3, n_looks=5, declared_by=" "),
              EvaluationError))
    ok("a plan with zero looks is refused",
       raises(lambda: evaluate.declare_plan(con3, n_looks=0, declared_by="a"),
              EvaluationError))
    ok("an unknown spending function is refused",
       raises(lambda: evaluate.declare_plan(con3, n_looks=5, declared_by="a",
                                            spending="whatever"), Exception))

    plan_id = evaluate.declare_plan(
        con3, n_looks=10, declared_by="analyst-1",
        note="weekly through the first quarter", declared_at=at(days=39))
    ok("a plan declares", plan_id > 0)
    ok("declaring a second plan for the same slice is refused",
       raises(lambda: evaluate.declare_plan(con3, n_looks=50, declared_by="a"),
              EvaluationError))
    ok("a plan is immutable — raising the budget after a bad look is the failure "
       "the budget exists to prevent",
       raises(lambda: con3.execute(
           "UPDATE evaluation_plans SET n_looks=99 WHERE id=?", (plan_id,)),
           sqlite3.IntegrityError))
    ok("a plan is never deleted",
       raises(lambda: con3.execute("DELETE FROM evaluation_plans WHERE id=?",
                                   (plan_id,)), sqlite3.IntegrityError))

    ev = evaluate.evaluate(con3, resamples=800)
    ok("with a plan the gate evaluates", ev.blocked_by is None, ev.blocked_by)
    ok("the threshold comes from the schedule, not from 1.96",
       ev.z_threshold > 1.96, ev.z_threshold)
    ok("an unrecorded look is marked as a dry run", not ev.recorded)
    ok("...and spends nothing",
       evaluate.looks_taken(con3, plan_id) == 0)
    ok("the first O'Brien-Fleming look is severe, as it should be",
       ev.look_index == 1 and ev.z_threshold > 3.0, ev.z_threshold)
    print("        " + ev.summary().replace("\n", "\n        "))

    ev_rec = evaluate.evaluate(con3, resamples=800, record_look=True,
                               looked_at=at(days=40))
    ok("a recorded look is written down", evaluate.looks_taken(con3, plan_id) == 1)
    ok("...and says so", ev_rec.recorded)
    ok("the next look is index 2",
       evaluate.evaluate(con3, resamples=800).look_index == 2)
    ok("a look is append-only",
       raises(lambda: con3.execute(
           "UPDATE evaluation_looks SET fired=1 WHERE plan_id=?", (plan_id,)),
           sqlite3.IntegrityError))
    ok("deleting a look would un-spend alpha, and is refused",
       raises(lambda: con3.execute("DELETE FROM evaluation_looks WHERE plan_id=?",
                                   (plan_id,)), sqlite3.IntegrityError))
    hist = evaluate.look_history(con3, plan_id)
    ok("the look history records what was seen and what threshold applied",
       len(hist) == 1 and hist[0]["z_threshold"] == ev_rec.z_threshold)

    print("\n[4a] Spending the whole budget, and what it costs\n")
    for _ in range(9):
        evaluate.evaluate(con3, resamples=200, record_look=True, looked_at=at(days=41))
    ok("ten looks exhaust a ten-look plan",
       evaluate.looks_taken(con3, plan_id) == 10)
    spent = evaluate.evaluate(con3, resamples=200)
    ok("the eleventh is refused: the schedule is exhausted",
       "exhausted" in (spent.blocked_by or ""), spent.blocked_by)
    ok("the budget is enforced by the schema too, not only in code",
       raises(lambda: con3.execute(
           "INSERT INTO evaluation_looks(plan_id, look_index, looked_at, "
           "n_observations, n_regimes, point, std_error, z_threshold, fired) "
           "VALUES(?,11,?,1,1,0.0,0.0,1.0,1)", (plan_id, at(days=42))),
           sqlite3.IntegrityError))
    zs = [h["z_threshold"] for h in evaluate.look_history(con3, plan_id)]
    ok("O'Brien-Fleming thresholds relax toward the final look",
       zs[0] > zs[-1], (zs[0], zs[-1]))
    ok("...and the last is still stricter than a nominal one-sided 1.645",
       zs[-1] > 1.645, zs[-1])
    print(f"        z thresholds: {' '.join(f'{z:.2f}' for z in zs)}")

    print("\n[4a-ii] The budget alpha is not the interval alpha\n")
    # These are different quantities. Sharing a parameter would narrow the
    # interval used to estimate the sampling spread, understate the standard
    # error, and make the gate easier to fire -- silently, and only for someone
    # who customised alpha.
    con5 = ledger.connect(":memory:", create=True)
    _, made5 = build(con5, n_regimes=12, per=10, seed=5)
    for pid, _, outcome in made5:
        evaluate.record_resolution(
            con5, pid, "resolved_yes" if outcome else "resolved_no", "m",
            recorded_at=at(days=40))
    evaluate.score_all(con5, as_of=at(days=40))
    evaluate.declare_plan(con5, n_looks=10, declared_by="a", alpha=0.20)

    con6 = ledger.connect(":memory:", create=True)
    _, made6 = build(con6, n_regimes=12, per=10, seed=5)
    for pid, _, outcome in made6:
        evaluate.record_resolution(
            con6, pid, "resolved_yes" if outcome else "resolved_no", "m",
            recorded_at=at(days=40))
    evaluate.score_all(con6, as_of=at(days=40))
    evaluate.declare_plan(con6, n_looks=10, declared_by="a", alpha=0.05)

    wide = evaluate.evaluate(con5, resamples=3000)
    narrow = evaluate.evaluate(con6, resamples=3000)
    ok("the reported interval is 95% regardless of the plan's alpha",
       abs((wide.ci_upper - wide.ci_lower) -
           (narrow.ci_upper - narrow.ci_lower)) < 0.02,
       (wide.ci_upper - wide.ci_lower, narrow.ci_upper - narrow.ci_lower))
    ok("a looser budget gives a LOWER z threshold, as it should",
       wide.z_threshold < narrow.z_threshold,
       (wide.z_threshold, narrow.z_threshold))
    ok("...which is the only thing the plan's alpha should change",
       abs(wide.ci_point - narrow.ci_point) < 0.02)
    print(f"        alpha 0.20 -> z {wide.z_threshold:.2f}, "
          f"alpha 0.05 -> z {narrow.z_threshold:.2f}; "
          f"interval widths {wide.ci_upper - wide.ci_lower:.4f} vs "
          f"{narrow.ci_upper - narrow.ci_lower:.4f}")

    print("\n[4b-i] Below twelve regimes, still refused\n")
    con4 = ledger.connect(":memory:", create=True)
    _, made4 = build(con4, n_regimes=4, per=10, seed=9)
    for pid, _, outcome in made4:
        evaluate.record_resolution(
            con4, pid, "resolved_yes" if outcome else "resolved_no", "m",
            resolved_at=at(days=40), recorded_at=at(days=40))
    evaluate.score_all(con4, as_of=at(days=40))
    evaluate.declare_plan(con4, n_looks=10, declared_by="analyst-1")
    ev4 = evaluate.evaluate(con4, resamples=800)
    ok("four regimes reports descriptive statistics", ev4.brier is not None)
    ok("...and says the gate is NOT EVALUABLE rather than giving an interval",
       ev4.blocked_by is not None and ev4.ci_lower is None, ev4.blocked_by)
    ok("...and does not claim the gate fired", not ev4.gate_fires)
    ok("a refused look spends nothing",
       evaluate.looks_taken(con4, evaluate.plan_for(con4)["id"]) == 0)
    print("        " + ev4.summary().replace("\n", "\n        "))
    ok("an empty record evaluates to nothing, not to zero",
       evaluate.evaluate(ledger.connect(":memory:", create=True)).n == 0)
    ok("calibration is reported separately from skill",
       len(evaluate.calibration(con3)) > 0)
    sl = evaluate.slices(con3)
    ok("slices show what would be averaged over", len(sl) == 1 and sl[0]["n"] == 120,
       sl)

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

    print("\n[5b] A corrected outcome is a revision, not a replacement\n")
    pid_r = made3[1][0]
    con3.execute("DELETE FROM scores WHERE forecast_id IN "
                 "(SELECT id FROM forecasts WHERE proposition_id=?)", (pid_r,))
    con3.commit()
    hist0 = evaluate.resolution_history(con3, pid_r)
    ok("the first resolution is revision 1", hist0[0]["revision"] == 1)
    ok("recording a second first-resolution is refused",
       raises(lambda: evaluate.record_resolution(con3, pid_r, "resolved_no", "x"),
              EvaluationError))
    ok("a revision with no adjudicator is refused",
       raises(lambda: evaluate.revise_resolution(
           con3, pid_r, "resolved_no", adjudication="changed my mind",
           adjudicator=""), EvaluationError))
    rid = evaluate.revise_resolution(
        con3, pid_r, "resolved_no",
        adjudication="UMA dispute upheld; the recorded vote was procedural",
        adjudicator="analyst-2", recorded_at=at(days=45))
    ok("a revision records", rid > 0)
    hist = evaluate.resolution_history(con3, pid_r)
    ok("both revisions are on the record", len(hist) == 2, hist)
    ok("the original outcome is still readable at revision 1",
       hist[0]["outcome"] == hist0[0]["outcome"] and hist[1]["outcome"] == "resolved_no",
       (hist[0]["outcome"], hist[1]["outcome"]))
    ok("the revision carries its reason and adjudicator",
       hist[1]["adjudicator"] == "analyst-2" and hist[1]["adjudication"])
    ok("the current view shows only the newest",
       con3.execute("SELECT outcome FROM current_resolutions WHERE proposition_id=?",
                    (pid_r,)).fetchone()[0] == "resolved_no")
    ok("resolutions are append-only",
       raises(lambda: con3.execute(
           "UPDATE resolutions SET outcome='void' WHERE proposition_id=?", (pid_r,)),
           sqlite3.IntegrityError))
    ok("a resolution is never deleted",
       raises(lambda: con3.execute(
           "DELETE FROM resolutions WHERE proposition_id=?", (pid_r,)),
           sqlite3.IntegrityError))
    ok("revising a proposition that was never resolved is refused",
       raises(lambda: evaluate.revise_resolution(
           con3, 99999, "resolved_no", adjudication="x", adjudicator="y"),
           EvaluationError))

    # Scores against the superseded revision are reported, not silently redone.
    evaluate.score_all(con3, as_of=at(days=46))
    evaluate.revise_resolution(
        con3, made3[2][0], "void",
        adjudication="the question was unresolvable as written",
        adjudicator="analyst-2", recorded_at=at(days=47))
    stale = evaluate.stale_scores(con3)
    ok("a forecast scored against a superseded revision is reported as stale",
       len(stale) == 1 and stale[0]["proposition_id"] == made3[2][0], stale)
    ok("...and says which revision it was scored against",
       stale[0]["scored_against"] < stale[0]["current_revision"])
    evaluate.score_all(con3, as_of=at(days=48))
    ok("rescoring clears it", evaluate.stale_scores(con3) == [])
    ok("...by adding a row, not by changing one",
       con3.execute("SELECT COUNT(*) FROM scores WHERE forecast_id=("
                    "SELECT id FROM forecasts WHERE proposition_id=? LIMIT 1)",
                    (made3[2][0],)).fetchone()[0] == 2)
    print(f"        {evaluate.resolution_history(con3, pid_r)[1]['adjudication']}")

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

#!/usr/bin/env python3
"""
End-to-end integration: screen -> registry -> ledger -> chain -> scoring ->
decision -> ablation, on one dataset, in one process.

Every earlier suite validates a module against fixtures shaped by hand for that
module. That is how two components both pass their own tests and still cannot be
joined: nothing had ever carried a row from the market screen all the way to a
scored decision. This suite does, and its job is to break on the seams.

Run: python3 tests/test_e2e.py
Stdlib only.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "phase0"))

import screen_markets  # noqa: E402
from spine import (ablation, chain, decision, evaluate, evidence,  # noqa: E402
                   ledger, models, refclass, registry, scoring, shadow,
                   sizing)
from spine.ablation import Variant  # noqa: E402
from spine.models import ReferenceClass  # noqa: E402
from spine.registry import RegistryError  # noqa: E402
from spine.scoring import Observation  # noqa: E402

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


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# a raw feed shaped like Gamma, including the rows that must be refused
# ---------------------------------------------------------------------------

def raw_feed() -> list[dict]:
    end = iso(NOW + timedelta(days=9))
    feed = [
        {
            "id": f"mk{i}",
            "question": f"Will proposal {i} be adopted by the committee?",
            "endDate": end,
            "liquidityNum": 40_000 + i,
            "volumeNum": 150_000,
            "description": (f"Resolves YES if proposal {i} is recorded as adopted "
                            "in the official minutes on or before the end date."),
            "clobTokenIds": json.dumps([f"tok{i}a", f"tok{i}b"]),
            "events": [{"id": f"ev{i}", "tags": [{"label": "Policy"}]}],
            "active": True, "closed": False,
        }
        for i in range(6)
    ]
    # The two rows registration must refuse, and one it must ignore outright.
    feed.append({"id": "no_deadline", "question": "Will it happen eventually?",
                 "liquidityNum": 50_000, "description": "Resolves YES if it happens."})
    feed.append({"id": "no_rules", "question": "Will the index close above 5000?",
                 "endDate": end, "liquidityNum": 50_000})
    feed.append({"question": "Untitled", "endDate": end, "description": "x"})
    return feed


def main() -> int:
    print("=" * 76)
    print("SPINE END-TO-END INTEGRATION")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)

    # ---------------------------------------------------- screen -> registry
    print("\n[1] The market screen's own output registers as contracts\n")
    from dataclasses import asdict

    markets = screen_markets.normalise(raw_feed(), NOW)
    rows = [asdict(m) for m in markets]
    ok("the screen carries settlement rules through normalisation",
       all(r["rules_text"] for r in rows if r["id"].startswith("mk")))
    ok("...and the outcome token, taking YES from the Gamma pair",
       rows[0]["outcome_token_id"] == "tok0a", rows[0]["outcome_token_id"])
    ok("the removed probability firewall leaves no field behind",
       "firewall_cap" not in rows[0])

    rep = registry.ingest_markets(con, rows, jurisdiction="GB", now=iso(NOW))
    print(f"        {rep.summary()}")
    ok("six well-formed markets registered", rep.inserted == 6, rep.inserted)
    reasons = dict(rep.skipped)
    ok("a market with no deadline is skipped with a reason",
       "no deadline" in reasons.get("no_deadline", ""), reasons)
    ok("a market with no rules text is skipped with a reason",
       "no resolution text" in reasons.get("no_rules", ""), reasons)
    ok("a market with no identifier is skipped with a reason",
       any("identifier" in r for _, r in rep.skipped))
    ok("nothing was registered with an invented placeholder rule",
       con.execute("SELECT COUNT(*) FROM contracts WHERE rules_text=''").fetchone()[0] == 0)

    again = registry.ingest_markets(con, rows, jurisdiction="GB", now=iso(NOW))
    ok("re-ingesting the same feed inserts nothing",
       again.inserted == 0 and again.already_present == 6, again.summary())

    # ---------------------------------------------------- eligibility
    print("\n[2] Eligibility is a jurisdiction on a date, not a market property\n")
    elig = {r[0] for r in con.execute(
        "SELECT DISTINCT eligibility_status FROM contracts")}
    ok("UK ingestion marks every contract close_only", elig == {"close_only"}, elig)
    ok("...and none of them are tradeable", registry.tradeable_contracts(con) == [])
    ok("the check time is recorded, not assumed permanent",
       con.execute("SELECT eligibility_checked_at FROM contracts LIMIT 1").fetchone()[0]
       == iso(NOW))
    ok("an undeclared jurisdiction is 'unknown', not 'tradeable'",
       registry.eligibility_for(None) == "unknown" and
       registry.eligibility_for("unknown") == "unknown")
    ok("a jurisdiction off the close-only list is tradeable",
       registry.eligibility_for("PT") == "tradeable")

    # ---------------------------------------------------- rules amendment
    print("\n[3] An amended rule is a new contract, not an edit\n")
    cid = con.execute("SELECT id FROM contracts WHERE market_id='mk0'").fetchone()[0]
    original = con.execute("SELECT rules_text FROM contracts WHERE id=?",
                           (cid,)).fetchone()[0]
    ok("unchanged text shows no divergence",
       not registry.divergence_check(con, cid, original))
    amended = original + " For the avoidance of doubt, a draft does not count."
    ok("amended text is detected", registry.divergence_check(con, cid, amended))

    before = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    amended_row = dict(rows[0])
    amended_row["rules_text"] = amended
    registry.ingest_markets(con, [amended_row], jurisdiction="GB", now=iso(NOW))
    after = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
    ok("the amendment lands as a new row", after == before + 1, (before, after))
    ok("...and the original text is untouched",
       con.execute("SELECT rules_text FROM contracts WHERE id=?",
                   (cid,)).fetchone()[0] == original)
    ok("...so a standing forecast still points at what it was made against",
       registry.divergence_check(con, cid, amended))
    ok("payout states record the UMA third outcome",
       json.loads(con.execute("SELECT payout_states FROM contracts WHERE id=?",
                              (cid,)).fetchone()[0])["UNKNOWN"] == 0.5)

    # ---------------------------------------------------- propositions
    print("\n[4] Propositions are bound to contracts by a reviewable act\n")
    pid = registry.ensure_proposition(
        con, "Proposal 0 is adopted by the committee",
        "Official minutes record adoption on or before the deadline",
        iso(NOW + timedelta(days=9)), "committee_adoption", "T1", now=iso(NOW))
    ok("a proposition registers", pid > 0)
    ok("registering it again returns the same id",
       registry.ensure_proposition(
           con, "Proposal 0 is adopted by the committee",
           "Official minutes record adoption on or before the deadline",
           iso(NOW + timedelta(days=9)), "committee_adoption", "T1") == pid)
    ok("an unknown horizon class is refused",
       raises(lambda: registry.ensure_proposition(con, "s", "c", iso(NOW), "f", "T9"),
              RegistryError))
    registry.bind(con, pid, cid, "exact", "e2e", now=iso(NOW))
    ok("the binding records who judged it and how well",
       con.execute("SELECT match_quality, reviewer FROM proposition_contract_binding "
                   "WHERE proposition_id=? AND contract_id=?", (pid, cid)).fetchone()
       == ("exact", "e2e"))
    ok("an unknown match quality is refused",
       raises(lambda: registry.bind(con, pid, cid, "probably_fine", "e2e"),
              RegistryError))
    ok("binding to a contract that does not exist is refused by the schema",
       raises(lambda: registry.bind(con, pid, 99_999, "exact", "e2e"),
              sqlite3.IntegrityError))

    # ---------------------------------------------------- evidence -> forecast
    print("\n[4b] A forecast actually driven by retrieved evidence\n")
    rc_preview = ReferenceClass("committee_adoption", 1, k=6, n=40,
                                exposure_units=40.0, alpha=1.0, beta=9.0)
    wire = evidence.ensure_source(con, "Global Wire", "wire", now=iso(NOW))
    herald = evidence.ensure_source(con, "Herald", "outlet",
                                    owner_group="Meridian", now=iso(NOW))
    desk = evidence.ensure_source(con, "Independent Desk", "outlet", now=iso(NOW))

    story = ("The committee recorded the measure as adopted at its Thursday "
             "sitting, and the minutes were published the same afternoon.")
    other = ("Members confirmed the chair had secured the votes needed before "
             "the sitting opened, according to people present in the room.")
    i1 = evidence.ingest_item(con, wire, story, first_seen_at=iso(NOW),
                              item_class="reportage", verification_lag_seconds=600,
                              now=iso(NOW))
    i2 = evidence.ingest_item(con, herald, story,
                              first_seen_at=iso(NOW + timedelta(minutes=30)),
                              item_class="reportage", verification_lag_seconds=600,
                              now=iso(NOW))
    i3 = evidence.ingest_item(con, desk, other,
                              first_seen_at=iso(NOW + timedelta(minutes=45)),
                              item_class="reportage", verification_lag_seconds=600,
                              now=iso(NOW))
    ok("the syndicated copy is detected on ingest", i2.duplicate_of == [i1.item_id])

    ev_items = [
        {"id": i1.item_id, "body": story, "anchor": "prop:adoption",
         "available_for_decision_at": iso(NOW + timedelta(minutes=10))},
        {"id": i2.item_id, "body": story, "anchor": "prop:adoption",
         "available_for_decision_at": iso(NOW + timedelta(minutes=40)),
         "attributes_to": "Global Wire"},
        {"id": i3.item_id, "body": other, "anchor": "prop:adoption",
         "available_for_decision_at": iso(NOW + timedelta(minutes=55))},
    ]
    ec = evidence.cluster_items(con, ev_items, cluster_version=1)[0]
    ok("the three items form one anchored cluster", len(ec.item_ids) == 3)

    claim_id, eff_src = evidence.record_claim(
        con, cluster_id=ec.cluster_id, cluster_version=1,
        assertion="The committee recorded the measure as adopted",
        authenticity="artifact_verified", extraction_fidelity="checked_faithful",
        establishes="underlying_fact", source_ids=[wire, herald, desk],
        available_for_decision_at=iso(NOW + timedelta(hours=1)),
        feature_version="ev-1", primary_artifact_id=i1.item_id,
        now=iso(NOW + timedelta(hours=1)))
    ok("three sources, one of them a syndicated copy, are worth fewer than three",
       eff_src.n_eff < 3.0, eff_src.summary())
    print(f"        {eff_src.summary()}")

    _, contrib = evidence.record_effect(
        con, claim_id=claim_id, contract_id=cid, horizon_days=9.0, llr=1.4,
        estimator="fitted_model", model_version="eff-1",
        conditioned_on_ref="manifest/I-at-t",
        available_for_decision_at=iso(NOW + timedelta(hours=1)),
        now=iso(NOW + timedelta(hours=1)))
    evidence.record_effect(
        con, claim_id=claim_id, contract_id=cid, horizon_days=9.0, llr=3.0,
        estimator="llm_proposed_unvalidated", model_version="eff-1",
        conditioned_on_ref="prompt/bundle-1",
        available_for_decision_at=iso(NOW + timedelta(hours=1)),
        now=iso(NOW + timedelta(hours=1)))

    decide_at = iso(NOW + timedelta(hours=2))
    contribs = evidence.usable_contributions(con, cid, decide_at, "eff-1", 9.0)
    ok("the forecast sees the fitted effect and not the LLM's proposal",
       len(contribs) == 1 and abs(contribs[0] - contrib.final) < 1e-12, contribs)
    early = evidence.usable_contributions(
        con, cid, iso(NOW + timedelta(minutes=30)), "eff-1", 9.0)
    ok("a forecast made before the evidence was verified sees nothing",
       early == [], early)

    ev_base = models.baseline_forecast(rc_preview, time_remaining=1.0)
    ev_fc = models.independent_forecast(ev_base, contribs, lambda_sig=1.0,
                                        lambda_version="v1")
    ok("evidence moved the estimate off the baseline",
       ev_fc.p_est_bp > ev_base.p_est_bp, (ev_base.p_est_bp, ev_fc.p_est_bp))
    ok("...and widened the interval rather than narrowing it",
       ev_fc.width_bp() > ev_base.width_bp())
    ok("a forecast with no retrieved evidence is exactly the baseline",
       models.independent_forecast(ev_base, early).p_est_bp == ev_base.p_est_bp)
    print(f"        baseline {ev_base.p_est_bp}bp -> evidence {ev_fc.p_est_bp}bp "
          f"(contribution {contrib.summary()})")

    # ---------------------------------------------------- the record
    print("\n[5] Twelve regimes of forecasts through the real ledger\n")
    rule_ref = refclass.declare_selection_rule(
        con, {"family": "committee_adoption",
              "include": "every measure reported out of the standing committee"},
        created_at=iso(NOW - timedelta(days=2)))
    rcid = refclass.freeze(
        con, class_name="committee_adoption", version=1,
        description="adoptions per committee-week",
        members=[refclass.Member(f"case{i}", iso(NOW - timedelta(days=300 - i)),
                                 1 if i < 6 else 0) for i in range(40)],
        selection_rule_ref=rule_ref, alpha=1.0, beta=9.0,
        prior_justification="family base rate near 0.10; Beta(1,9) has mean 0.10",
        exposure_units=40.0, exposure_unit_name="case-week",
        exposure_window_days=7, censoring_note="right-censored at deadline",
        frozen_at=iso(NOW - timedelta(days=1)))
    ok("the reference class derives its counts from its roster",
       (lambda a: a["ok"] and a["n"] == 40 and a["k"] == 6)(
           refclass.audit(con, rcid)), refclass.audit(con, rcid))
    ok("...and loads back as the model consumes it",
       refclass.load(con, rcid).exposure_units == 40.0)

    rc = ReferenceClass("committee_adoption", 1, k=6, n=40,
                        exposure_units=40.0, alpha=1.0, beta=9.0)
    rng = random.Random(11)
    n_regimes, per_regime = 12, 10
    registered, truths = [], {}

    for g in range(n_regimes):
        for i in range(per_regime):
            key = f"g{g}_q{i}"
            truth = rng.uniform(0.05, 0.95)
            truths[key] = truth
            created = NOW + timedelta(hours=g * 24 + i)
            p_id = registry.ensure_proposition(
                con, f"Proposal {key} is adopted", "Minutes record adoption",
                iso(created + timedelta(days=9)), "committee_adoption", "T1",
                now=iso(created))

            base = models.baseline_forecast(rc, time_remaining=1.0)
            # Evidence the forecaster actually saw, in log-odds, pushing the
            # baseline toward the truth by a bounded amount.
            contribution = models.logit(min(0.99, max(0.01, truth))) - \
                models.logit(base.p_est_bp / 10000)
            fc = models.independent_forecast(base, [contribution * 0.9],
                                             lambda_sig=1.0, lambda_version="v1")
            mh = ledger.put_manifest(
                con, "forecast_inputs",
                {"reference_class": "committee_adoption@1",
                 "contributions": [round(contribution * 0.9, 6)],
                 "question": key},
                iso(created))
            fh = ledger.register_forecast(
                con, proposition_id=p_id, contract_id=cid,
                p_est_bp=fc.p_est_bp, p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp,
                uncertainty_method=fc.uncertainty_method,
                min_width_bp=300, p_base_bp=base.p_est_bp,
                forecast_kind="independent",
                reference_class_version_id=rcid, inputs_manifest_hash=mh,
                model_version="e2e-1", regime_id=f"regime{g}",
                created_at=iso(created), source="model")
            registered.append((key, f"regime{g}", p_id, fh, fc, base))

    ok(f"{n_regimes * per_regime} forecasts registered through the ledger",
       len(registered) == n_regimes * per_regime)
    ok("every forecast interval satisfies its declared minimum width",
       all(f.p_hi_bp - f.p_lo_bp >= 300 for _, _, _, _, f, _ in registered))
    ok("a forecast whose interval is narrower than its minimum is refused",
       raises(lambda: ledger.register_forecast(
           con, proposition_id=pid, contract_id=cid, p_est_bp=5000, p_lo_bp=4990,
           p_hi_bp=5010, uncertainty_method="x", min_width_bp=300, p_base_bp=5000,
           forecast_kind="independent", reference_class_version_id=rcid,
           inputs_manifest_hash=registered[0][3] and ledger.put_manifest(
               con, "forecast_inputs", {"q": "narrow"}, iso(NOW)),
           model_version="e2e-1", regime_id="regime0", created_at=iso(NOW),
           source="model"), sqlite3.IntegrityError))
    ok("an independent forecast carrying a market price is refused",
       raises(lambda: ledger.register_forecast(
           con, proposition_id=pid, contract_id=cid, p_est_bp=5000, p_lo_bp=4000,
           p_hi_bp=6000, uncertainty_method="x", min_width_bp=0, p_base_bp=5000,
           p_market_bp=5100, forecast_kind="independent",
           reference_class_version_id=rcid,
           inputs_manifest_hash=ledger.put_manifest(
               con, "forecast_inputs", {"q": "peeked"}, iso(NOW)),
           model_version="e2e-1", regime_id="regime0", created_at=iso(NOW),
           source="model"), ledger.LedgerError))

    # ---------------------------------------------------- chain
    print("\n[6] The chain covers the whole record, not a sample\n")
    v = chain.verify(con)
    ok("chain intact over every registered forecast",
       v.ok and v.length == len(registered), v.summary())
    ok("an unanchored chain says so", v.unanchored_tail == v.length)
    chain.anchor(con, "rfc3161", "tsa.example/receipt-1", "base64-token", iso(NOW))
    v2 = chain.verify(con)
    ok("anchoring the head covers the record to that point",
       v2.anchored_through == v2.length and v2.unanchored_tail == 0, v2.summary())

    ok("an in-place edit is refused before verification is even consulted",
       raises(lambda: con.execute(
           "UPDATE forecasts SET model_version='e2e-2' WHERE id=40"),
           sqlite3.IntegrityError))
    # Simulate an attacker with direct file access, who is not bound by triggers.
    con.execute("DROP TRIGGER forecasts_no_update")
    con.execute("UPDATE forecasts SET model_version='e2e-2' WHERE id=40")
    con.commit()
    v3 = chain.verify(con)
    ok("editing a committed field mid-record breaks verification there",
       not v3.ok and v3.first_break == 40, v3.summary())
    con.execute("UPDATE forecasts SET model_version='e2e-1' WHERE id=40")
    con.commit()
    ok("restoring the field restores the chain", chain.verify(con).ok)
    print(f"        {chain.verify(con).summary()}")

    r = ledger.reconstruct(con, registered[0][3])
    ok("a forecast reconstructs with its manifest and frozen class",
       r["manifest_content"] and r["reference_class"]["class_name"] == "committee_adoption")

    # ---------------------------------------------------- outcomes -> scoring
    print("\n[7] Outcomes, then scoring over regimes\n")
    obs = []
    for key, regime, p_id, fh, fc, base in registered:
        outcome = 1 if rng.random() < truths[key] else 0
        con.execute(
            "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
            "resolved_at, recorded_at) VALUES(?,?,?,?,?)",
            (p_id, "resolved_yes" if outcome else "resolved_no", "minutes",
             iso(NOW + timedelta(days=30)), iso(NOW + timedelta(days=30))))
        obs.append(Observation(p=fc.p_est_bp / 10000, outcome=outcome,
                               p_base=base.p_est_bp / 10000,
                               regime_id=regime, key=key))
    con.commit()
    ok("every proposition resolved once",
       con.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0] == len(registered))
    ok("a second resolution for the same proposition is refused",
       raises(lambda: con.execute(
           "INSERT INTO resolutions(proposition_id, outcome, resolution_source, "
           "recorded_at) VALUES(?,?,?,?)",
           (registered[0][2], "resolved_no", "revision", iss := iso(NOW))),
           sqlite3.IntegrityError))

    ci = scoring.regime_bootstrap_ci(obs, resamples=2000)
    ok("the bootstrap runs at twelve regimes", ci.n_regimes == 12, ci.n_regimes)
    ok("evidence that moved the forecast toward the truth beats the baseline",
       ci.fires, f"{ci.point:+.4f} [{ci.lower:+.4f},{ci.upper:+.4f}]")
    ok("eleven regimes is refused, not warned about",
       raises(lambda: scoring.regime_bootstrap_ci(
           [o for o in obs if o.regime_id != "regime11"]), scoring.ScoringError))
    rb = scoring.estimate_r_between(obs)
    print(f"        BSS {scoring.bss(obs):+.4f}   paired {ci.point:+.4f} "
          f"[{ci.lower:+.4f}, {ci.upper:+.4f}]   r_between {rb:.4f} "
          f"(n_eff ceiling {scoring.n_eff_ceiling(rb):.0f})")

    # ---------------------------------------------------- scores in the record
    print("\n[7b] The record scores itself\n")
    run = evaluate.score_all(con, as_of=iso(NOW + timedelta(days=60)))
    ok("every forecast gets a score row, scorable or not",
       run.total == len(registered), run.summary())
    ok("all of them are scorable here", run.scored == len(registered))
    db_obs = evaluate.observations(con)
    ok("observations rebuilt from the database match the ones held in memory",
       len(db_obs) == len(obs) and
       abs(scoring.brier(db_obs) - scoring.brier(obs)) < 1e-12,
       (scoring.brier(db_obs), scoring.brier(obs)))
    unplanned = evaluate.evaluate(con, resamples=800)
    ok("without a declared budget the gate will not fire, however good the data",
       unplanned.blocked_by and "no evaluation plan" in unplanned.blocked_by,
       unplanned.blocked_by)

    evaluate.declare_plan(con, n_looks=10, declared_by="e2e",
                          declared_at=iso(NOW + timedelta(days=60)))
    ev = evaluate.evaluate(con, resamples=800)
    ok("with a plan the gate evaluates", ev.blocked_by is None, ev.blocked_by)
    ok("...and names the slice it averaged over",
       len(evaluate.slices(con)) == 1, evaluate.slices(con))

    # The point of the whole sequential apparatus, on one record: a plain 95%
    # bound fires, and the first of ten budgeted looks does not.
    ok("the fixed 95% bound would have fired here", ci.fires, ci.lower)
    ok("...and the first budgeted look does not, at z=%.2f" % ev.z_threshold,
       not ev.gate_fires, ev.summary())
    ok("an unrecorded look spends nothing",
       evaluate.looks_taken(con, evaluate.plan_for(con)["id"]) == 0)
    print("        " + ev.summary().replace("\n", "\n        "))
    print(f"        fixed 95% lower bound {ci.lower:+.4f} > 0 would have fired; "
          f"look 1/10 demands z={ev.z_threshold:.2f}")

    # ---------------------------------------------------- decision
    print("\n[8] A decision under the close-only posture\n")
    best = max(registered, key=lambda t: t[4].p_est_bp)
    fc = best[4]
    # The decision walks a RECORDED book, retrieved point-in-time -- not a list
    # built at the call site, which is how a backtest quietly fills at prices
    # nobody was offering.
    snap = shadow.record_book(
        con, cid, bids=[(2800, 500.0), (2700, 1200.0)],
        asks=[(3000, 200.0), (3100, 800.0), (3300, 2000.0)],
        captured_at=iso(NOW), source="fixture", capture_lag_seconds=2)
    live = shadow.book_at(con, cid, iso(NOW + timedelta(minutes=1)))
    ok("the decision retrieves the book that was available to it",
       live is not None and live.snapshot_id == snap)
    ok("a decision before the book was usable would have had none",
       shadow.book_at(con, cid, iso(NOW - timedelta(seconds=1))) is None)
    book = live.asks

    # Limits are DERIVED (§11.2), not invented at the call site. Every earlier
    # version of this test passed made-up numbers, which checked that arbitrary
    # limits are enforced rather than that correct ones are computed.
    size = sizing.position_limit(loss_budget_usd=1000.0, book=live, side="YES",
                                 p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp)
    ok("the position limit is derived from the book and the interval",
       size.max_notional_usd > 0, size.summary())
    ok("...and names what bound it", size.binding in
       ("loss budget", "liquidity", "loss budget then uncertainty",
        "liquidity then uncertainty"), size.binding)
    cap = sizing.concentration_cap_usd(1000.0, n_linked=0)
    used = sizing.cluster_exposure_used_usd(con, best[3])
    ok("nothing is committed against this cluster yet", used == 0.0)
    print(f"        {size.summary()}")
    d = decision.decide(
        p_est_bp=fc.p_est_bp, p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp, side="YES",
        book=book, intended_size_usd=min(500.0, size.max_notional_usd),
        costs_bp=100.0, eligibility_status="close_only",
        max_notional_usd=size.max_notional_usd,
        cluster_exposure_used_usd=used, cluster_exposure_cap_usd=cap,
        mode=decision.PAPER)
    ok("a paper decision is explicitly a simulation", d.is_simulation)
    ok("...and says so in a note that travels with it",
       any("SIMULATED" in n for n in d.notes), d.notes)
    d_live = decision.decide(
        p_est_bp=fc.p_est_bp, p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp, side="YES",
        book=book, intended_size_usd=min(500.0, size.max_notional_usd),
        costs_bp=100.0, eligibility_status="close_only",
        max_notional_usd=size.max_notional_usd,
        cluster_exposure_used_usd=used, cluster_exposure_cap_usd=cap,
        mode=decision.LIVE)
    ok("the same decision in live mode abstains on eligibility",
       not d_live.permitted and "close_only" in (d_live.abstain_reason or ""),
       d_live.abstain_reason)

    fid = con.execute("SELECT id FROM forecasts WHERE forecast_hash=?",
                      (best[3],)).fetchone()[0]
    decision.record(con, forecast_id=fid, contract_id=cid, decided_at=iso(NOW),
                    d=d, costs_bp=100.0, book_snapshot_ref="book/e2e-1")
    ok("the paper decision persists against a close_only contract",
       con.execute("SELECT COUNT(*) FROM trade_decisions WHERE mode='paper'")
       .fetchone()[0] == 1)
    ok("the live abstention persists with its reason",
       decision.record(con, forecast_id=fid, contract_id=cid, decided_at=iso(NOW),
                       d=d_live, costs_bp=100.0) > 0)
    ok("a decision that does not say whether it was real is not recordable",
       raises(lambda: con.execute(
           """INSERT INTO trade_decisions
              (forecast_id, contract_id, decided_at, permitted, abstain_reason,
               max_notional_usd, cluster_exposure_cap_usd, eligibility_status)
              VALUES (?,?,?,0,'no mode given',100.0,100.0,'close_only')""",
           (fid, cid, iso(NOW))), sqlite3.IntegrityError))
    ok("a LIVE permitted decision on a close_only contract is refused by the schema",
       raises(lambda: con.execute(
           """INSERT INTO trade_decisions
              (forecast_id, contract_id, decided_at, permitted, mode,
               max_notional_usd, cluster_exposure_cap_usd, eligibility_status)
              VALUES (?,?,?,1,'live',100.0,100.0,'close_only')""",
           (fid, cid, iso(NOW))), sqlite3.IntegrityError))
    ok("a refusal without a reason is refused by the schema",
       raises(lambda: con.execute(
           """INSERT INTO trade_decisions
              (forecast_id, contract_id, decided_at, permitted, mode,
               max_notional_usd, cluster_exposure_cap_usd, eligibility_status)
              VALUES (?,?,?,0,'paper',100.0,100.0,'close_only')""",
           (fid, cid, iso(NOW))), sqlite3.IntegrityError))

    # ---------------------------------------------------- shadow execution
    print("\n[8b] Shadow execution against the same book\n")
    o_agg = shadow.record_order(
        con, contract_id=cid, book_snapshot_id=snap, side="YES",
        style="aggressive", limit_price_bp=3300,
        # The size the DECISION reached, not one chosen here: a shadow fill that
        # replays a different size than the decision took is not shadowing it.
        intended_size_usd=d.intended_size_usd,
        placed_at=iso(NOW), decision_id=con.execute(
            "SELECT id FROM trade_decisions ORDER BY id LIMIT 1").fetchone()[0])
    f_agg = shadow.aggressive_fill(live, "YES", d.intended_size_usd, 3300)
    shadow.record_fill(con, o_agg, f_agg, recorded_at=iso(NOW))
    ok("an aggressive fill pays through the inside price",
       f_agg.avg_price_bp > live.best_ask_bp, f_agg.summary())
    ok("...and the decision's own estimate matched the same walk, to the cent",
       abs(f_agg.avg_price_bp - d.expected_acquisition_bp) < 1e-6,
       (f_agg.avg_price_bp, d.expected_acquisition_bp))
    ok("...which only holds because the shadow replays the decision's size",
       abs(f_agg.filled_usd - d.intended_size_usd) < 1e-9,
       (f_agg.filled_usd, d.intended_size_usd))

    o_pass = shadow.record_order(
        con, contract_id=cid, book_snapshot_id=snap, side="YES", style="passive",
        limit_price_bp=2800, intended_size_usd=500.0, placed_at=iso(NOW),
        cancel_at=iso(NOW + timedelta(minutes=5)), cancel_latency_ms=250.0)
    f_pass = shadow.passive_fill(live, "YES", 500.0, 2800,
                                 traded_at_level_usd=300.0)
    shadow.record_fill(con, o_pass, f_pass, recorded_at=iso(NOW + timedelta(minutes=5)))
    ok("resting behind 500 of queue with 300 traded is not a fill",
       f_pass.filled_usd == 0.0, f_pass.summary())

    later = shadow.record_book(
        con, cid, bids=[(2500, 600.0)], asks=[(2700, 600.0)],
        captured_at=iso(NOW + timedelta(minutes=20)), source="fixture")
    adverse = shadow.markout(con, con.execute(
        "SELECT id FROM shadow_fills WHERE order_id=?", (o_agg,)).fetchone()[0],
        later, computed_at=iso(NOW + timedelta(minutes=25)))
    ok("the aggressive fill is marked out against a later book",
       abs(adverse - (2900.0 - 2600.0)) < 1e-9, adverse)
    rep = shadow.adverse_selection_report(con)
    ok("the execution record covers both orders", rep.n_orders == 2)
    ok("...and reports a fill rate below one", rep.fill_rate == 0.5, rep.summary())
    print(f"        {rep.summary()}")

    # ---------------------------------------------------- ablation
    print("\n[9] Does the evidence beat the price on this record?\n")
    market = Variant("market", [
        Observation(p=max(0.01, min(0.99, 0.5 + 0.55 * (truths[o.key] - 0.5))),
                    outcome=o.outcome, p_base=o.p_base,
                    regime_id=o.regime_id, key=o.key)
        for o in obs])
    full = Variant("independent", obs)
    c = ablation.beats_market(full, market, resamples=1500)
    ok("the comparison is paired on every shared question",
       c.n_paired == len(obs), c.n_paired)
    ok("it runs against the price, not a placeholder", c.reference == "market")
    ok("comparing against an unresolved question set is refused",
       raises(lambda: ablation.beats_market(
           full, Variant("short", market.observations[:5])), Exception))

    # The comparison above returns "no detectable effect" on this record, and
    # that is the Phase 0 power result arriving in practice rather than a bug:
    # 120 observations over 12 regimes cannot resolve a Brier difference of
    # ~0.01. Against a market that is genuinely much worse, the same machinery
    # fires -- which is what separates "no effect" from "cannot see".
    dull = Variant("uninformed", [
        Observation(p=0.5, outcome=o.outcome, p_base=o.p_base,
                    regime_id=o.regime_id, key=o.key) for o in obs])
    c_dull = ablation.beats_market(full, dull, resamples=1500)
    ok("the same comparison fires against a market carrying no information",
       c_dull.contributes, c_dull.verdict())
    need = screen_markets.required_n_eff(abs(c.delta_brier) / 0.25)
    print(f"\n        a {c.delta_brier:+.4f} Brier difference needs n_eff ~{need:.0f} "
          f"to resolve; this record has {len(obs)} observations in 12 regimes")
    print("\n" + ablation.report([c, c_dull]))

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

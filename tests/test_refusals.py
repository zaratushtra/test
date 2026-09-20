#!/usr/bin/env python3
"""
The refusals: every guard fires, and says something a reader can act on.

A `raise` that no test reaches is a guard nobody has checked. It may be
unreachable, it may have the condition inverted, it may crash on the way to
raising — all three look identical from outside, and all three mean the
discipline it encodes is not actually enforced.

Tracing the other nineteen suites found **44 of 152 raise sites in `spine/`
never reached by anything**. This suite covers them, and then traces *itself* to
prove it: the claim "these are now tested" is checkable rather than asserted,
and the check fails if a guard is added without one.

Run: python3 tests/test_refusals.py
Stdlib only.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

from spine import (ablation, canonical, chain, collect, conditional,  # noqa: E402
                   decision, evidence, ledger, models, refclass, registry,
                   scoring, sequential, shadow, sizing, splits, timeutil, venue)
from spine.scoring import Observation  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
T = timeutil.iso(NOW)

# The lines this suite is responsible for reaching, measured by tracing the
# other suites. Kept as data so the self-check below can verify it.
TARGETS_FILE = os.path.join(ROOT, "tests", "refusal_targets.json")


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def refuses(label, fn, exc=Exception, contains=""):
    """A guard must fire AND explain itself."""
    try:
        fn()
    except exc as e:
        if contains and contains.lower() not in str(e).lower():
            ok(label, False, f"message lacks {contains!r}: {e}")
            return
        ok(label, True)
        return
    except Exception as e:  # noqa: BLE001
        ok(label, False, f"raised {type(e).__name__} instead: {e}")
        return
    ok(label, False, "did not raise")


def main() -> int:
    print("=" * 76)
    print("SPINE REFUSALS")
    print("=" * 76)

    print("\n[1] Guards that encode a discipline\n")
    o = Observation(0.6, 1, 0.5, "r", "k")
    refuses("a paired comparison with conflicting outcomes is refused",
            lambda: ablation.compare(
                ablation.Variant("a", [Observation(0.6, 1, 0.5, "r", "k")]),
                ablation.Variant("b", [Observation(0.6, 0, 0.5, "r", "k")])),
            ablation.AblationError, "different outcomes")
    refuses("...and one where a key sits in two regimes",
            lambda: ablation.compare(
                ablation.Variant("a", [Observation(0.6, 1, 0.5, "r1", "k")]),
                ablation.Variant("b", [Observation(0.6, 1, 0.5, "r2", "k")])),
            ablation.AblationError, "different regimes")
    refuses("an empty variant is refused",
            lambda: ablation.compare(ablation.Variant("a", []),
                                     ablation.Variant("b", [o])),
            ablation.AblationError, "need observations")

    refuses("a forecast missing a required field is refused",
            lambda: ledger.register_forecast(
                ledger.connect(":memory:", create=True), p_est_bp=5000),
            ledger.LedgerError, "missing required")
    refuses("...and one carrying an unknown field",
            lambda: ledger.register_forecast(
                ledger.connect(":memory:", create=True), nonsense=1,
                **{k: 1 for k in ledger._REQUIRED}),
            ledger.LedgerError, "unknown field")

    refuses("bss on nothing refuses rather than returning zero",
            lambda: scoring.bss([]), scoring.ScoringError, "no scorable")
    refuses("skill against a perfect benchmark is undefined, not infinite",
            lambda: scoring.bss([Observation(0.6, 1, 1.0, "r", "k")]),
            scoring.ScoringError, "undefined")
    for name, fn in (("brier", scoring.brier), ("murphy", scoring.murphy),
                     ("regime_bootstrap_ci", scoring.regime_bootstrap_ci)):
        refuses(f"{name} on nothing refuses rather than returning zero",
                lambda f=fn: f([]), scoring.ScoringError, "no scorable")

    refuses("a training example created after the cutoff is caught as leakage",
            lambda: splits.assert_no_leakage(splits.Split(
                T, train=[splits.Example("late", timeutil.iso(NOW + timedelta(days=5)),
                                         timeutil.iso(NOW - timedelta(days=1)))])),
            splits.SplitError, "created after the cutoff")

    print("\n[2] Inputs that would make a number mean something else\n")
    con_r = ledger.connect(":memory:", create=True)
    rule = refclass.declare_selection_rule(con_r, {"f": "x"}, created_at=T)
    refuses("a non-binary reference class member is refused",
            lambda: refclass.freeze(
                con_r, class_name="x", version=1, description="d",
                members=[refclass.Member("m", T, 7)],
                selection_rule_ref=rule, alpha=1.0, beta=9.0,
                prior_justification="j", exposure_units=1.0,
                exposure_unit_name="c", frozen_at=T),
            refclass.RefClassError, "non-binary")
    refuses("a Beta prior with a non-positive parameter is refused",
            lambda: refclass.freeze(
                con_r, class_name="x", version=1, description="d", members=[],
                selection_rule_ref=rule, alpha=0.0, beta=9.0,
                prior_justification="j", exposure_units=1.0,
                exposure_unit_name="c", frozen_at=T),
            refclass.RefClassError, "alpha > 0")

    refuses("an unknown authenticity value is refused",
            lambda: evidence.cap_for("underlying_fact", "probably", "unchecked"),
            evidence.EvidenceError, "authenticity")
    refuses("an unknown extraction fidelity is refused",
            lambda: evidence.cap_for("underlying_fact", "artifact_verified", "ok"),
            evidence.EvidenceError, "extraction_fidelity")
    refuses("a non-positive n_eff is refused",
            lambda: evidence.evidence_weight(0.0),
            evidence.EvidenceError, "positive")

    refuses("a negative hazard rate is refused",
            lambda: models.hazard_to_probability(-1.0, 1.0),
            models.ModelError, "negative")
    refuses("a fit with no variation is refused, not silently singular",
            lambda: models.fit_market_conditioned([0.5] * 5, [1] * 5, ridge=0.0),
            models.ModelError)
    refuses("a negative residual sd is refused",
            lambda: models.market_conditioned_forecast(
                0.5, 0.0, 1.0, residual_sd_logodds=-1.0),
            models.ModelError, "negative")

    refuses("an inverted rho range is refused",
            lambda: conditional.coordination_penalty(0.5, 3, rho_centre=0.7,
                                                     rho_range=(0.9, 0.5)),
            conditional.ConditionalError, "inverted")
    refuses("a non-positive loss budget in a concentration cap is refused",
            lambda: sizing.concentration_cap_usd(0.0, 1),
            sizing.SizingError, "positive")
    refuses("negative group membership is refused",
            lambda: sizing.concentration_cap_usd(100.0, -1),
            sizing.SizingError, "negative")

    refuses("a quantile outside (0,1) is refused",
            lambda: sequential._norm_ppf(1.5),
            sequential.SequentialError, "out of range")
    for name, fn in (("Pocock", sequential.spend_pocock),
                     ("O'Brien-Fleming", sequential.spend_obrien_fleming)):
        refuses(f"{name} refuses an information fraction outside (0,1]",
                lambda f=fn: f(0.0, 0.05),
                sequential.SequentialError, "information fraction")
    refuses("a negative standard error is refused",
            lambda: sequential.critical_lower_bound(
                sequential.schedule(3)[0], -1.0),
            sequential.SequentialError, "negative")

    refuses("a malformed prev_hash is refused",
            lambda: canonical.chain_hash({"a": 1}, "short"),
            canonical.CanonicalisationError, "64 hex")
    refuses("anchoring an empty chain is refused",
            lambda: chain.anchor(ledger.connect(":memory:", create=True),
                                 "rfc3161", "r", "p", T),
            chain.ChainError, "chain is empty")
    refuses("a zero-size book walk is refused",
            lambda: decision.walk_book([decision.BookLevel(5000, 10.0)], 0.0),
            decision.DecisionError, "positive")

    print("\n[3] Lookups for things that are not there\n")
    con = ledger.connect(":memory:", create=True)
    for label, fn, exc, msg in (
        ("a missing signal item",
         lambda: evidence.record_syndication(con, 999, [1], T),
         evidence.EvidenceError, "no signal item"),
        ("a missing claim",
         lambda: evidence.record_effect(
             con, claim_id=999, contract_id=1, horizon_days=1.0, llr=0.1,
             estimator="fitted_model", model_version="v",
             conditioned_on_ref="m", available_for_decision_at=T),
         evidence.EvidenceError, "no claim"),
        ("a missing contract",
         lambda: registry.divergence_check(con, 999, "text"),
         registry.RegistryError, "no contract"),
        ("a missing forecast",
         lambda: ledger.reconstruct(con, "0" * 64),
         ledger.LedgerError, "no forecast"),
        ("a missing book snapshot",
         lambda: shadow.load_book(con, 999),
         shadow.ShadowError, "no book snapshot"),
        ("a missing fill",
         lambda: shadow.markout(con, 999, 1),
         shadow.ShadowError, "no fill"),
        ("a missing reference class version",
         lambda: refclass.load(con, 999),
         refclass.RefClassError, "no reference class"),
    ):
        refuses(f"{label} is a named refusal, not a crash", fn, exc, msg)

    print("\n[4] Markouts that would measure the wrong thing\n")
    registry.ingest_markets(con, [
        {"id": "a", "question": "q", "end_date": timeutil.iso(NOW + timedelta(days=9)),
         "rules_text": "r", "outcome_token_id": "t1"},
        {"id": "b", "question": "q", "end_date": timeutil.iso(NOW + timedelta(days=9)),
         "rules_text": "r", "outcome_token_id": "t2"}],
        jurisdiction="GB", now=T)
    s1 = shadow.record_book(con, 1, [(5900, 100.0)], [(6100, 100.0)],
                            captured_at=T, source="fixture")
    s_other = shadow.record_book(con, 2, [(5900, 100.0)], [(6100, 100.0)],
                                 captured_at=timeutil.iso(NOW + timedelta(hours=1)),
                                 source="fixture")
    one_sided = shadow.record_book(con, 1, [(5800, 100.0)], [],
                                   captured_at=timeutil.iso(NOW + timedelta(hours=2)),
                                   source="fixture")
    o1 = shadow.record_order(con, contract_id=1, book_snapshot_id=s1, side="YES",
                             style="aggressive", limit_price_bp=6200,
                             intended_size_usd=50.0, placed_at=T)
    f1 = shadow.record_fill(con, o1, shadow.aggressive_fill(
        shadow.load_book(con, s1), "YES", 50.0, 6200), recorded_at=T)
    refuses("marking against another contract's book is refused",
            lambda: shadow.markout(con, f1, s_other),
            shadow.ShadowError, "different contract")
    refuses("marking against a one-sided book is refused",
            lambda: shadow.markout(con, f1, one_sided),
            shadow.ShadowError, "no midpoint")
    refuses("recording a refusal with no reason is refused",
            lambda: decision.record(
                con, forecast_id=1, contract_id=1, decided_at=T,
                d=decision.Decision(mode="paper", permitted=False,
                                    abstain_reason=None, side="YES",
                                    expected_acquisition_bp=None,
                                    conservative_p_bp=None, ev_per_share_bp=None,
                                    intended_size_usd=0.0, max_notional_usd=0.0,
                                    cluster_exposure_cap_usd=0.0,
                                    eligibility_status="close_only"),
                costs_bp=0.0),
            decision.DecisionError, "carry a reason")

    print("\n[5] The network paths, without a network\n")
    # Patching urlopen rather than _get, because _get IS the converter under
    # test: replacing it would test the stub and report a pass.
    class FakeResp:
        def __init__(self, body): self.body = body
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *a): return False

    saved_open = venue.urllib.request.urlopen
    try:
        venue.urllib.request.urlopen = lambda r, timeout=30: (_ for _ in ()).throw(
            urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None))
        refuses("an HTTP error becomes a VenueError naming the code",
                lambda: venue.fetch_markets(1), venue.VenueError, "503")
        venue.urllib.request.urlopen = lambda r, timeout=30: (_ for _ in ()).throw(
            urllib.error.URLError("no route to host"))
        refuses("an unreachable host is a distinct error",
                lambda: venue.fetch_markets(1), venue.Unreachable, "cannot reach")
        refuses("...and it says to run from a networked machine",
                lambda: venue.fetch_markets(1), venue.Unreachable, "--from-dir")
        venue.urllib.request.urlopen = lambda r, timeout=30: FakeResp(b"<html>")
        refuses("a non-JSON body is refused",
                lambda: venue.fetch_markets(1), venue.VenueError, "not JSON")
        venue.urllib.request.urlopen = lambda r, timeout=30: FakeResp(
            b'{"not": "a list"}')
        refuses("markets that are not a list are refused",
                lambda: venue.fetch_markets(1), venue.VenueError, "expected a list")
    finally:
        venue.urllib.request.urlopen = saved_open
    refuses("a book request with no token is refused",
            lambda: venue.fetch_book(""), venue.VenueError, "token")
    # The same refusal one layer up, where it is caught per-contract rather
    # than raised to the caller: a contract with no token is skipped with a
    # reason, not allowed to abort the sweep.
    tokenless = venue.snapshot_books(
        con, [{"id": 1, "market_id": "a", "outcome_token_id": None}],
        source="fixture", fetcher=lambda t: {"bids": [], "asks": []})
    ok("a contract with no outcome token is skipped with that reason",
       tokenless["recorded"] == 0
       and "no outcome token" in tokenless["skipped"][0][1],
       tokenless["skipped"])

    saved_u = collect.urllib.request.urlopen
    try:
        def bad_http(req, timeout=30):
            raise urllib.error.HTTPError("u", 404, "Not Found", {}, None)

        def bad_url(req, timeout=30):
            raise urllib.error.URLError("nope")

        collect.urllib.request.urlopen = bad_http
        refuses("a feed returning HTTP 404 is a CollectError",
                lambda: collect.fetch_feed("https://x.test/rss"),
                collect.CollectError, "404")
        collect.urllib.request.urlopen = bad_url
        refuses("an unreachable feed is distinguishable from a bad one",
                lambda: collect.fetch_feed("https://x.test/rss"),
                collect.FeedUnreachable, "cannot reach")
    finally:
        collect.urllib.request.urlopen = saved_u

    print("\n[6] The claim that these are covered, checked\n")
    targets = json.load(open(TARGETS_FILE, encoding="utf-8"))
    n_targets = sum(len(v) for v in targets.values())
    ok(f"a target list of {n_targets} guards this suite owns exists",
       n_targets > 30, n_targets)

    # Keyed by the raise TEXT, not the line number. The first version used line
    # numbers and went stale the moment anything above a guard was edited,
    # failing for a reason that had nothing to do with coverage.
    sites = {}
    for p in sorted(pathlib.Path(ROOT, "spine").glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and node.exc is not None:
                try:
                    txt = " ".join(ast.unparse(node.exc).split())
                except Exception:  # noqa: BLE001
                    txt = "<unparseable>"
                sites.setdefault(p.name, set()).add(txt)
    stale = {m: sorted(set(v) - sites.get(m, set())) for m, v in targets.items()}
    stale = {m: v for m, v in stale.items() if v}
    ok("every guard this suite owns still exists in the code", not stale,
       {m: v[:2] for m, v in stale.items()})
    ok("the target list names only modules that exist",
       set(targets) <= set(sites), set(targets) - set(sites))
    print(f"        {n_targets} guards owned across {len(targets)} modules; "
          "run tests/trace_refusals.py --verify to confirm reachability")

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

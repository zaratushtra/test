"""Shared fixture helpers. Not a suite; imported by suites that need a record."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger, models, refclass, registry, timeutil  # noqa: E402

NOW = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def build_two_forecasts(con) -> tuple[str, str]:
    """A contract, a frozen class and two registered forecasts. Returns their hashes."""
    registry.ingest_markets(con, [{
        "id": "mk-s", "question": "Will it pass?",
        "end_date": timeutil.iso(NOW + timedelta(days=9)),
        "rules_text": "Resolves on the recorded vote.",
        "outcome_token_id": "tok-s"}], jurisdiction="GB",
        now=timeutil.iso(NOW))
    cid = con.execute("SELECT id FROM contracts").fetchone()[0]

    rule = refclass.declare_selection_rule(
        con, {"family": "committee_adoption", "include": "reported measures"},
        created_at=timeutil.iso(NOW - timedelta(days=2)))
    rcid = refclass.freeze(
        con, class_name="committee_adoption", version=1, description="d",
        members=[refclass.Member(f"c{i}", timeutil.iso(NOW - timedelta(days=300 - i)),
                                 1 if i < 6 else 0) for i in range(40)],
        selection_rule_ref=rule, alpha=1.0, beta=9.0,
        prior_justification="family base rate near 0.10",
        exposure_units=40.0, exposure_unit_name="case-week",
        frozen_at=timeutil.iso(NOW - timedelta(days=1)))
    rc = refclass.load(con, rcid)

    hashes = []
    for i in range(2):
        created = NOW + timedelta(hours=i)
        pid = registry.ensure_proposition(
            con, f"Proposition {i}", "Recorded vote",
            timeutil.iso(created + timedelta(days=9)), "committee_adoption",
            "T1", now=timeutil.iso(created))
        fc = models.baseline_forecast(rc, time_remaining=1.0)
        mh = ledger.put_manifest(con, "forecast_inputs", {"q": i},
                                 timeutil.iso(created))
        hashes.append(ledger.register_forecast(
            con, proposition_id=pid, contract_id=cid, p_est_bp=fc.p_est_bp,
            p_lo_bp=fc.p_lo_bp, p_hi_bp=fc.p_hi_bp,
            uncertainty_method=fc.uncertainty_method, min_width_bp=0,
            p_base_bp=fc.p_est_bp, forecast_kind="independent",
            reference_class_version_id=rcid, inputs_manifest_hash=mh,
            model_version="sz-1", regime_id=f"r{i}",
            created_at=timeutil.iso(created), source="model"))
    return hashes[0], hashes[1]

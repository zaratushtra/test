#!/usr/bin/env python3
"""
Phase 0 — vintage data coverage audit.

§8.1's availability-time discipline applied to *data series*: a calibration must
use the value as it was **first published**, not as later revised. Replaying today's revised series
against 2015 gives the model numbers nobody had in 2015, and every backtest comes
out flattering and wrong.

This audits whether such vintage series actually exist for the indicators Stage 2
needs. ALFRED (the vintage arm of FRED) publishes a vintage-date list per series;
a series with vintages spanning the calibration window is usable, one without is not.

Known limitation, and it is the point of running this: ALFRED covers US macro well.
IMF, World Bank and EIA have no comparable public vintage archive. For any non-US
indicator this script will report UNAVAILABLE, and that is a genuine finding about
Stage 2's feasibility, not a bug here.

Needs a free FRED API key:  export FRED_API_KEY=...
Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

FRED_BASE = "https://api.stlouisfed.org/fred"

# UNVERIFIED: candidate FRED series for the Stage 2 pressure vectors. Series IDs
# were written without network access — confirm each resolves before trusting a
# coverage verdict. Replace with the project's real indicator list.
DEFAULT_SERIES = {
    "debt_to_gdp_us": "GFDEGDQ188S",
    "gdp_us": "GDP",
    "industrial_production_us": "INDPRO",
    "fiscal_balance_pct_gdp_us": "FYFSGDA188S",
    "cpi_us": "CPIAUCSL",
    "unemployment_us": "UNRATE",
}

# Indicators the design needs that ALFRED is not expected to cover. Listed so the
# report names them rather than silently omitting them.
KNOWN_GAPS = [
    ("energy_import_dependency", "EIA — no public vintage archive"),
    ("demographic_dependency_ratio", "World Bank / UN WPP — revised in place"),
    ("military_age_cohort", "UN WPP — revised in place"),
    ("sovereign_debt_non_us", "IMF WEO — vintages exist only as archived PDF releases"),
    ("munitions_production_rate", "no systematic public series"),
]


def fetch_vintages(series_id: str, api_key: str, timeout: int = 30) -> list[str]:
    qs = urllib.parse.urlencode(
        {"series_id": series_id, "api_key": api_key, "file_type": "json"}
    )
    url = f"{FRED_BASE}/series/vintagedates?{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "spine-phase0-vintage/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("vintage_dates", [])


def assess(vintages: list[str], start: str, end: str) -> dict:
    inside = [v for v in vintages if start <= v <= end]
    if not vintages:
        return {"status": "UNAVAILABLE", "vintages_total": 0, "vintages_in_window": 0}
    status = "OK" if len(inside) >= 8 else ("THIN" if inside else "OUT_OF_WINDOW")
    return {
        "status": status,
        "vintages_total": len(vintages),
        "vintages_in_window": len(inside),
        "first_vintage": vintages[0],
        "last_vintage": vintages[-1],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE Phase 0 vintage coverage audit")
    ap.add_argument("--start", default="2005-01-01")
    ap.add_argument("--end", default="2024-12-31")
    ap.add_argument("--series-json", help="JSON file: {name: fred_series_id}")
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    api_key = os.environ.get("FRED_API_KEY", "").strip()
    if not api_key:
        print("FRED_API_KEY is not set. Get a free key at "
              "https://fredaccount.stlouisfed.org/apikeys and export it.",
              file=sys.stderr)
        return 2

    series = DEFAULT_SERIES
    if args.series_json:
        with open(args.series_json, encoding="utf-8") as fh:
            series = json.load(fh)

    print(f"\n{'='*70}\nSPINE PHASE 0 — VINTAGE COVERAGE AUDIT\n{'='*70}")
    print(f"calibration window: {args.start} .. {args.end}\n")
    print(f"{'indicator':<32}{'series':<18}{'status':<14}{'vintages':>10}")
    print("-" * 74)

    results = {}
    for name, sid in series.items():
        try:
            vintages = fetch_vintages(sid, api_key)
            r = assess(vintages, args.start, args.end)
        except urllib.error.HTTPError as e:
            r = {"status": f"HTTP_{e.code}", "vintages_total": 0, "vintages_in_window": 0}
        except Exception as e:  # noqa: BLE001
            r = {"status": "ERROR", "error": str(e),
                 "vintages_total": 0, "vintages_in_window": 0}
        r["series_id"] = sid
        results[name] = r
        print(f"{name:<32}{sid:<18}{r['status']:<14}{r['vintages_in_window']:>10}")
        time.sleep(0.5)  # be polite to the API

    print(f"\n{'-'*74}\nKNOWN GAPS — not auditable here, and material to §5.3\n{'-'*74}")
    for name, why in KNOWN_GAPS:
        print(f"  {name:<34}{why}")

    ok = sum(1 for r in results.values() if r["status"] == "OK")
    total = len(results)
    print(f"\n{'='*70}")
    print(f"auditable series with usable vintage coverage: {ok}/{total}")
    verdict = "PASS" if ok == total else ("PARTIAL" if ok else "FAIL")
    print(f"GATE: {verdict}")
    if verdict != "PASS":
        print("\n  Stage 2 cannot be honestly calibrated on indicators with no vintage\n"
              "  archive. Either restrict Stage 2 to indicators that have one, or\n"
              "  reduce the structural claim to what the available data supports.")

    os.makedirs(args.outdir, exist_ok=True)
    path = os.path.join(args.outdir, "vintage_audit.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "as_of": date.today().isoformat(),
                "window": {"start": args.start, "end": args.end},
                "results": results,
                "known_gaps": [{"indicator": n, "reason": w} for n, w in KNOWN_GAPS],
                "auditable_ok": ok,
                "auditable_total": total,
                "gate": verdict,
            },
            fh,
            indent=2,
        )
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPINE Phase 0 — feasibility audits

Three audits, any of which can stop the project. Stdlib-only Python 3.10+; no installs.

| Script | Audit | Needs network to |
|---|---|---|
| `screen_markets.py` | (a) Market screen + T1 independence survey | `gamma-api.polymarket.com` |
| `audit_vintage.py` | (b) Vintage data coverage for Stage 2 | `api.stlouisfed.org` (FRED key) |
| `throughput.py` | (c) Per-forecast cost and timeline projection | none |

## Run

```bash
cd phase0

# (a) market screen — inspect the API shape first if extraction looks wrong
python3 screen_markets.py --dump-schema --limit 5
python3 screen_markets.py --limit 3000 --weekly-size 15 --target-bss 0.10

# (b) vintage coverage
export FRED_API_KEY=...          # free: https://fredaccount.stlouisfed.org/apikeys
python3 audit_vintage.py --start 2005-01-01 --end 2024-12-31

# (c) throughput — time five real forecasts, then report
python3 throughput.py time --tier T1 --question "Will X happen by <date>?"
python3 throughput.py report --analyst-hours 20 --r-bar 0.05
```

Outputs land in `out/`: `market_screen.csv`, `market_screen_summary.json`,
`vintage_audit.json`, `throughput_log.jsonl`.

## What each gate means

**(a)** The screen does not ask "are there enough markets." It asks whether there are
enough *independent* markets, because `n_eff = n / (1 + (n-1)·r̄)` and correlated
markets contribute almost nothing. The sweep output reports the optimal weekly basket
size — see the non-monotonicity note in `../PHASE0-FINDINGS.md`, which is the most
important thing to read before interpreting a result.

**(b)** Stage 2 calibration needs point-in-time (vintage) series. ALFRED covers US
macro; IMF, World Bank and EIA have no comparable public vintage archive. A `PARTIAL`
verdict is expected and is a real constraint on the structural claim, not a bug.

**(c)** Stage 3 is the predicted bottleneck. If it dominates the per-forecast cost,
partial automation there is the highest-leverage engineering work in the project.

## Caveats

- `screen_markets.py` field names in `FIELD_CANDIDATES` were written **without access
  to the live Gamma API** (blocked from the authoring sandbox). The script warns if
  fewer than half of markets yield both an end date and liquidity. Fix the candidate
  lists from `--dump-schema` output if so — it is a two-minute change.
- `audit_vintage.py` series IDs in `DEFAULT_SERIES` are **unverified candidates**.
  Replace with the project's real indicator list via `--series-json`.
- Both were tested against synthetic fixtures only. Clustering, sweep, zone
  classification, edge cases and exit codes are exercised; live API shape is not.

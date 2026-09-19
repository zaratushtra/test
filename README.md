# SPINE

Structural forecasting with auditable pre-registration, evaluated against
prediction markets.

**Canonical design:** [`SPINE-DESIGN-V2.md`](SPINE-DESIGN-V2.md)

## Status

| Phase | State | Evidence |
|---|---|---|
| 0 · Feasibility | **partly resolved** | Jurisdiction declared: UK, **paper-only** posture (design §2.1). Market screen still needs network access from a host outside the build sandbox. PolyBench audit complete (failed — see `PHASE0-FINDINGS.md`). |
| 1 · Ledger & registry | **passed** | 33/33 `tests/test_phase1.py`, 36/36 `db/test_schema_v2.py` — see `docs/PHASE1-REPORT.md` |
| 2 · Narrow forecasting | **partial** | 67/67 `tests/test_phase2.py` — scoring and decision layers done; registry population and ablations blocked on Phase 0. See `docs/PHASE2-REPORT.md` |
| 3–6 | specification | — |

**Paper only.** Operations are UK-based, where Polymarket is close-only on both
frontend and API. Live trading is out of scope; the decision layer is a research
instrument that measures whether an edge *would have* survived execution and
costs. No account, wallet or KYC is involved at any point.

Nothing here demonstrates predictive skill. Phases 0–2 cover feasibility,
integrity and plumbing only.

## Layout

```
SPINE-DESIGN-V2.md        canonical design and roadmap
PHASE0-FINDINGS.md        Phase 0 results, including the PolyBench retraction
db/schema_v2.sql          20 STRICT tables; the guards live here, not in Python
db/test_schema_v2.py      probe suite — every finding an external review raised
spine/                    canonicalisation, hash chain, evidence ledger
phase0/                   market screen, vintage audit, throughput pilot
phase1/serial_dependence.py   the analysis that invalidated the original gate
phase1/sequential_peeking.py  what unbudgeted peeking costs, and the fix
tests/test_phase1.py      Phase 1 validation
tests/test_phase2.py      Phase 2 validation
docs/                     phase reports, the external review, and its disposition
docs/DATA-SOURCES.md      free/OSS sources and what each actually requires
docs/history/             superseded v1 documents
```

## Validation

```bash
python3 db/test_schema_v2.py      # schema guarantees
python3 tests/test_phase1.py      # ledger, chain, availability discipline
python3 tests/test_phase2.py      # scoring, decision, abstention
python3 phase1/serial_dependence.py    # power and serial dependence analysis
python3 phase1/sequential_peeking.py   # cost of peeking; alpha-spending check
```

Stdlib only; no installs. Requires Python 3.10+ and SQLite 3.37+ (STRICT tables).

## Two results worth knowing before reading anything else

**The probability firewall was removed.** Capping the probability of an event by
horizon class confuses it with confidence in the estimate; the v1 schema rejected
a legitimate 2% ten-day forecast. Estimate, uncertainty and trading permission
are now separate. Cap exposure, never the probability of reality.

**The original validation gate was invalid, not merely slow.** A cluster
bootstrap over weeks cannot see a component shared across all weeks, and fires on
pure noise up to 38% of the time — running longer makes it marginally worse.
Scoring now groups by `regime_id` and requires at least 12 independent regimes —
and `spine/scoring.py` exports no week-level bootstrap at all, so the invalid
path cannot be taken by accident.

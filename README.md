# SPINE

Structural forecasting with auditable pre-registration, evaluated against
prediction markets.

**Canonical design:** [`SPINE-DESIGN-V2.md`](SPINE-DESIGN-V2.md)
**How to run it:** [`docs/RUNNING.md`](docs/RUNNING.md)

## Status

| Phase | State | Evidence |
|---|---|---|
| 0 · Feasibility | **partly resolved** | Jurisdiction declared: UK, **paper-only** posture (design §2.1). Market screen still needs network access from a host outside the build sandbox. PolyBench audit complete (failed — see `PHASE0-FINDINGS.md`). |
| 1 · Ledger & registry | **passed** | 33/33 `tests/test_phase1.py`, 39/39 `db/test_schema_v2.py` — see `docs/PHASE1-REPORT.md` |
| 2 · Narrow forecasting | **passed on fixtures** | 67/67 + 43/43 + 54/54 end-to-end. Scoring, decision, models, ablations and the contract registry are built and joined; only *live* market data is blocked on network access. See `docs/PHASE2-REPORT.md` |
| — · Evidence pipeline | **built, fixtures only** | 68/68 `tests/test_evidence.py` — ingestion, deduplication, effective sources, claims, contradictions, influence budget. See `docs/EVIDENCE-REPORT.md` |
| — · Shadow execution | **built, fixtures only** | 48/48 `tests/test_shadow.py` — recorded books, queue position, cancellation latency, adverse selection. See `docs/SHADOW-EXECUTION-REPORT.md` |
| — · Evaluation loop | **built, fixtures only** | 87/87 `tests/test_evaluate.py` — scores, exclusions, settlement divergence, the dependence DAG. See `docs/EVALUATION-REPORT.md` |
| — · Signal collection | **built, fixtures only** | 42/42 `tests/test_collect.py` — predeclared anchored queries, RSS/Atom, hostile-input guards, provenance |
| — · Venue access & cycle | **built, offline-validated** | 38/38 `tests/test_venue.py` — `run_cycle.py` runs the whole cycle in one command. See `docs/RUNNING.md` |
| 3 · Prospective evaluation | blocked | Needs live data and calendar time: ≥12 regimes of pre-registered forecasts |
| 4–6 | specification | — |

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
spine/registry.py         screened markets -> contracts; rules-version identity
spine/evidence.py         signals -> claims -> contract effects; the influence budget
spine/shadow.py           recorded books, queue fills, markouts, adverse selection
spine/timeutil.py         one canonical timestamp; point-in-time comparison depends on it
spine/evaluate.py         resolutions -> scores -> verdict; settlement divergence
spine/collect.py          query-driven RSS/Atom collection, anchored to propositions
spine/venue.py            Gamma + CLOB read access; no auth path exists
run_cycle.py              one operating cycle: screen -> register -> record books
tests/test_phase1.py      Phase 1 validation
tests/test_phase2.py      Phase 2 validation — scoring, decision
tests/test_phase2b.py     Phase 2 validation — models, ablations
tests/test_evidence.py    evidence pipeline validation
tests/test_shadow.py      shadow execution validation
tests/test_timeutil.py    timestamp canonicalisation and the ordering bug
tests/test_docs.py        the documents' claims, checked against the code
tests/test_evaluate.py    scoring the record, exclusions, the DAG
tests/test_collect.py     feed parsing, hostile input, provenance
tests/test_venue.py       venue parsing and the cycle, offline
tests/test_e2e.py         end-to-end: screen -> registry -> ledger -> score -> decide
run_tests.py              runs every suite
docs/                     phase reports, the external review, and its disposition
docs/DATA-SOURCES.md      free/OSS sources and what each actually requires
docs/history/             superseded v1 documents
```

## Validation

```bash
python3 run_tests.py              # all eleven suites, 590 checks
python3 run_tests.py --docs       # and check what the documents claim
```

The second one matters more than it sounds. These documents make specific,
checkable assertions — suite counts, file layouts, schema versions, measured
figures that are cited as reasons for design choices — and every one can quietly
become false. A document that is confidently wrong is worse than one that says
nothing, because it gets believed.

Individually:

```bash
python3 tests/test_timeutil.py    # timestamp canonicalisation
python3 db/test_schema_v2.py      # schema guarantees
python3 tests/test_phase1.py      # ledger, chain, availability discipline
python3 tests/test_phase2.py      # scoring, decision, abstention
python3 tests/test_phase2b.py     # models, hazard, ablations
python3 tests/test_evidence.py    # ingestion, dedup, n_eff, influence budget
python3 tests/test_shadow.py      # books, queue position, markouts
python3 tests/test_collect.py     # feeds, XML guards, provenance
python3 tests/test_evaluate.py    # scores, settlement, dependence DAG
python3 tests/test_venue.py       # book parsing, units, the cycle
python3 tests/test_e2e.py         # the whole pipeline on one dataset
python3 phase1/serial_dependence.py    # power and serial dependence analysis
python3 phase1/sequential_peeking.py   # cost of peeking; alpha-spending check
```

The end-to-end suite is the one that matters most and the one that was missing
longest. Everything else validates a module against fixtures shaped for that
module, which is how two components can both pass and still not join.

Stdlib only; no installs. Requires Python 3.10+ and SQLite 3.37+ (STRICT tables).

## Four results worth knowing before reading anything else

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

**Lexical similarity finds duplication, not events.** Two independent reports of
one committee vote shared two content words out of thirty-five — Jaccard 0.061,
shingle overlap 0.000. No threshold separates that from unrelated text, because
independent reporting of the same event *is* lexically unrelated; that is what
makes it independent. Signal collection is therefore query-driven from the
contract registry, and text similarity is used only for the job it is good at:
collapsing syndicated copies. See `docs/EVIDENCE-REPORT.md` §3.

**Point-in-time correctness was format-fragile.** Every retrieval in this
project is a string comparison, and `datetime.isoformat()` drops the fractional
part on a whole second — so `'...T13:00:00Z'` sorts *after* `'...T13:00:00.000Z'`
and a forecast recorded at an instant was invisible to a decision made at that
same instant. One canonical format, seventeen schema `CHECK ... GLOB`
constraints, and a ledger that validates rather than coerces the hash-committed
field. See `docs/EVALUATION-REPORT.md` §1.

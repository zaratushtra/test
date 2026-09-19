# Evaluation Report — closing the loop, and a timestamp bug

**Date:** 19 Sep 2026
**Status:** **Built and validated.** `spine/evaluate.py` and `spine/timeutil.py`; 43/43 in
`tests/test_evaluate.py`, 33/33 in `tests/test_timeutil.py`, plus 5 checks in the end-to-end run.
**What it closes:** three tables the project defined and never populated — `scores`, `settlements`,
`forecast_edges` — plus the step nobody had written: building observations *from the database*
rather than from a list assembled by hand. `spine/scoring.py` could score anything except the
record.

---

## 1. The bug this work found

Every point-in-time query in this project is a **string comparison**:
`available_for_decision_at <= ?`, `created_at <= ?`, `computed_at <= ?`. That is correct and fast —
for exactly as long as every timestamp has the same shape.

They did not. `datetime.isoformat()` omits the fractional part when microseconds are zero, so a row
written on a whole second is stamped `2026-09-19T13:00:00Z` while the cutoff it is compared against
is `2026-09-19T13:00:00.000Z`. Lexicographically, `'.'` (0x2E) sorts before `'Z'` (0x5A):

```python
>>> '2026-09-19T13:00:00Z' <= '2026-09-19T13:00:00.000Z'
False
```

**A forecast recorded at an instant was invisible to a retrieval at that same instant.** Not wrong
by an hour — wrong by a format, silently, and only for rows that happened to land on a whole
second. Roughly one row in a thousand under normal timing, and *every* row in any fixture or
scheduled job that uses round times. Which is how it surfaced: a test cutoff at exactly `NOW + 1h`
saw one forecast where two existed.

Point-in-time correctness is one of the three things this project claims to do. The comparison it
rests on was format-fragile in a way that no test had reason to look at.

### The fix, in three places

1. **`spine/timeutil.py`** — one canonical form, `YYYY-MM-DDTHH:MM:SS.sssZ`, UTC and milliseconds
   always. Liberal on the way in (`parse()` accepts offsets, missing fractions, lowercase `z`),
   strict on the way out. Six copies of `_now`/`_iso`/`_parse` lived in six modules and disagreed on
   exactly this case; they are now one.
2. **Schema `CHECK ... GLOB`** on every column that gets compared — 17 constraints. Timestamps that
   are *merely recorded* are deliberately left free: a source's `claimed_published_at` or a venue's
   own timestamp is evidence about the outside world, and forcing it into our shape would be
   rewriting what the source said.
3. **`register_forecast()` validates rather than coerces.** `created_at` is hash-committed, so
   silently reshaping it would change what the chain attests to. Every other write path
   canonicalises; this one refuses, with a message that explains the ordering problem.

Schema version bumped to 4, which `ledger.connect()` enforces.

---

## 2. Closing the evaluation loop

### Exclusions are counted, never dropped

Every forecast gets a `scores` row — scorable or not — with a reason when not:

| reason | meaning |
|---|---|
| `unresolved` | no resolution recorded yet |
| `void` / `disputed` / `unresolvable` | the outcome cannot be scored against |
| `label_not_available` | the outcome was not yet knowable at the scoring time |

Silently skipping unscorable forecasts shrinks the denominator, and a shrinking denominator makes
skill look better. So `ScoringRun` reports the **exclusion rate next to the score**, and a high one
is itself the finding.

### Scoring is point-in-time in two senses at once

`as_of` gates both *which forecasts exist yet* and *whether their labels were knowable*. The first
is easy to overlook and is now pinned by a test: a forecast made after the scoring moment is not
part of that record. On the fixture, scoring at day 12 splits cleanly — 21 scored, 99 excluded as
`label_not_available`, partitioning the window exactly.

### A score that changes silently would undo the chain

Already-scored forecasts are never recomputed. A resolution arriving later does not rewrite an
existing exclusion. The whole point of the hash chain upstream is that the record does not move; a
scoring pass that rewrote history would undo it one table lower down.

### Comparisons stay within a slice

`slices()` lists every (kind, model version, event family) combination with its count, so
"evaluate the record" cannot quietly mean "average over things that should not be averaged". A
Brier average across two model versions measures the mixture.

### The gate refuses rather than caveats

Below twelve regimes, `evaluate()` reports the descriptive statistics and says **NOT EVALUABLE**. It
does not return a narrower interval with a warning attached. Phase 1 measured a 43.5% false-positive
rate at one regime — a number produced there is worse than no number, because a number gets quoted
and a refusal does not.

---

## 3. Settlement may disagree with the outcome, and that is the point

`record_settlement()` deliberately accepts a payout that contradicts the research outcome. A UMA
"Unknown" settles at 0.50 on a proposition that resolved cleanly YES, and forcing consistency would
erase exactly the discrepancy worth knowing about.

`settlement_divergence()` finds them. Every row is a case where **the instrument did not measure the
question** — the concrete, after-the-fact version of §10.1's "material mismatch", carrying the
binding quality that was asserted beforehand. On the fixture: a contract bound `exact` that paid 0.5
on `resolved_yes`.

---

## 4. Score correlation is not loss correlation

`forecast_edges` keeps a separate DAG per `dependence_kind`, with cycle detection within each.

Two forecasts can be scored almost identically and lose money at different times, or be scored
independently and blow up together. The schema was already built for this; nothing had used it.
Tested directly: the same pair may cycle in the `loss` graph while being refused as a cycle in the
`score` graph, so "these are correlated" is always a statement about *which* correlation.

---

## 5. What this does not establish

- **Every number here is from fixtures.** The evaluation machinery is validated; nothing has been
  evaluated.
- **`scores` has no append-only trigger**, unlike `forecasts` and `claims`. Re-scoring is prevented
  in code rather than in the schema — weaker than the guarantees elsewhere, and worth tightening
  before real scores exist.
- **A corrected resolution has no path.** `resolutions` is `UNIQUE(proposition_id)`, so a disputed
  outcome later adjudicated cannot be recorded without deleting a row. That is a real gap: the
  design wants adjudication to be recorded, not applied by replacement.
- **The seventeen `GLOB` checks cover the columns compared today.** A new comparison on an
  unconstrained column would reintroduce the same class of bug, which is an argument for adding the
  check when adding the column.

---

## 6. Next

1. **An append-only trigger on `scores`**, matching `forecasts` and `claims`.
2. **A resolution-correction path** that records an adjudication rather than replacing a row.
3. **Real forecasts to score**, which needs the event-family decision and live data.

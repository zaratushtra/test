# Evaluation Report — closing the loop, and a timestamp bug

**Date:** 19 Sep 2026
**Status:** **Built and validated.** `spine/evaluate.py` and `spine/timeutil.py`; 87/87 in
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

Three conditions block the gate **by name**, rather than attaching a warning to a number: no
declared evaluation plan, fewer than twelve regimes, and stale scores left by an unprocessed
resolution revision. Phase 1 measured a 43.5% false-positive rate at one regime — a number produced
there is worse than no number, because a number gets quoted and a refusal does not.

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

## 4b. The readout was committing the error the project had measured

The first version of `evaluate()` reported a gate verdict from a plain 95% bootstrap interval. That
is fine once. It is not fine on a schedule — and `run_cycle.py` is designed to run on a schedule, so
**every run was a look**. `phase1/sequential_peeking.py` had already measured what that costs: a
weekly check of a fixed bound turns a ~3% gate into **19.3%** over a year.

`spine/sequential.py` had existed since Phase 2 with the alpha-spending machinery in it. Nothing
connected it to the thing that does the looking. The project had written the fix, measured the
problem, documented both, and then built the readout the wrong way anyway.

The gate now takes its threshold from a declared schedule:

- **`evaluation_plans`** — one per slice, immutable and undeletable by trigger. Raising the budget
  after a disappointing look is exactly the failure a budget exists to prevent, so the schema
  refuses it rather than trusting that nobody will. A plan must name who declared it.
- **`evaluation_looks`** — append-only, undeletable (deleting one would un-spend alpha already
  spent), and capped at the declared count by a trigger, not only in code.
- **Looks are free until recorded.** `evaluate()` is a dry run by default and says so; recording is
  a deliberate, irreversible act.

The effect, on the same record, is the whole argument in two lines:

```
fixed 95% lower bound +0.1157 > 0        would have fired
look 1/10, O'Brien-Fleming, z = 6.09     does not fire
```

Thresholds across a ten-look schedule: **6.09, 4.23, 3.40, 2.95, 2.68, 2.52, 2.42, 2.35, 2.31,
2.28** — severe early, relaxing toward the end, and the last still stricter than a nominal one-sided
1.645. Early stopping becomes rare and meaningful; almost all power is preserved for the full
schedule.

This also converts one of the four outstanding human decisions from a note in a document into an
enforced precondition. The number of looks cannot be chosen after seeing the data, because the gate
will not evaluate at all until it has been chosen.

---

## 5. Corrections are revisions — and fixing that fixed a latent bug

The first version of this report listed two gaps. Both are now closed, and closing them surfaced a
third problem that neither had described.

**`resolutions` was `UNIQUE(proposition_id)`**, so a disputed outcome later adjudicated could only
be recorded by deleting the original — erasing the fact that the outcome was ever in doubt. That is
exactly what §7.3 forbids for claims, and there is no reason the rule should weaken one table over.
Resolutions are now revisions: `revise_resolution()` appends, requires a written reason *and* a
named adjudicator, and the schema refuses a revision without both. The table is append-only and
undeletable by trigger, and a `current_resolutions` view carries the live outcome so no query has to
remember to pick the newest.

**`scores` now records which revision it scored against**, and is append-only by trigger. A
correction therefore produces a *new* score row; the old one is not invalidated, because it was
correct against the revision it names. `stale_scores()` lists forecasts whose newest score predates
the newest resolution — reported rather than recomputed on read, since the number of forecasts a
correction disturbed is itself worth seeing.

**The latent bug:** scoring previously skipped any forecast that already had a score row. A forecast
scored while `unresolved` therefore stayed excluded **forever**, even once its outcome landed. It
took keying scores by resolution revision to see it — revision 0 means "no resolution yet", so the
arrival of a real outcome is now a revision change and triggers a score. Nothing in the old tests
caught this, because the old test *asserted the buggy behaviour* as if it were a discipline.

---

## 6. What this does not establish

- **Every number here is from fixtures.** The evaluation machinery is validated; nothing has been
  evaluated.
- **The seventeen `GLOB` checks cover the columns compared today.** A new comparison on an
  unconstrained column would reintroduce the same class of bug, which is an argument for adding the
  check at the same time as the column.
- **`stale_scores()` is advisory.** Nothing forces a rescore before the next evaluation, so a
  revision left unscored silently keeps the old observation in the record. A gate on `evaluate()`
  would be stricter.

---

## 7. Next

1. ~~Gate `evaluate()` on an empty `stale_scores()`.~~ Done — a correction can no longer be
   evaluated around.
2. **Real forecasts to score**, which needs the event-family decision and live data.
3. **Choose `n_looks`.** The gate now refuses to evaluate until someone does, which is the right
   failure mode but still a decision only a human can make.

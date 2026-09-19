# Evidence Pipeline Report — §6 and §7 implemented

**Date:** 19 Sep 2026
**Status:** **Built and validated on fixtures.** `spine/evidence.py`, 68/68 in
`tests/test_evidence.py`, plus 8 checks in the end-to-end run where evidence actually drives a
forecast.
**What it closes:** v2 called §6 "the missing core of v1". It was also missing from v2 — seven
schema tables with no code behind them. There is now code, and the tables are populated by it.

---

## 1. The shape of the thing

```
signal_items ──cluster──► event_clusters ──► claims ──► claim_contract_effects ──► forecast
     │                                          │              │
  five timestamps                    four verification     the influence budget
  one governs retrieval                  questions          binds the product
```

Each arrow is a place where a plausible shortcut produces a flattering number. The module is
organised around refusing those shortcuts.

---

## 2. Four disciplines, each enforced rather than documented

### 2.1 Duplication is not corroboration

Two outlets carrying the same wire story are one piece of evidence. A naive source count gets
*more* confident the more widely a single origin is copied — exactly backwards.

Byte-identical content arriving under a second masthead is detected at ingest and recorded as
`syndication` correlation 1.0, at the time it was observed. Measured on the fixture: three copies
of one wire story give **n_eff = 1.00**. Adding a desk with its own reporting raises it to 1.36 — and the result reports that 3 of its 6
pairs are assumed rather than measured.

### 2.2 An unmeasured pair is unknown, not independent

`n_eff_sources` is Kish over the error-correlation matrix, `n / (1 + (n−1)r̄)`. The consequential
decision is the fallback when a pair has no recorded estimate:

| situation | correlation | basis |
|---|---|---|
| identical artifact | 1.00 | `syndication` |
| recorded measurement | measured | `observed_error_agreement` |
| same owner group | 0.75 | `ownership` |
| **nothing known** | **0.30** | `assumed` |

Never zero. Assuming independence is the flattering direction, and the flattering direction must
never be the default. `EffectiveSources.leans_on_assumptions` reports when assumed pairs outnumber
measured ones, so a confident-looking n_eff cannot hide what it rests on.

n_eff is also clamped at the headcount. Negatively-correlated errors can push the formula above `n`,
which is mathematically fine and epistemically reckless: an *estimate* should never be allowed to
claim more independent sources than there are sources.

### 2.3 Correlations are point-in-time

`source_error_correlation` carries `computed_at` in its primary key, and retrieval filters
`computed_at <= as_of`. A correlation learned in March cannot sharpen a decision made in February.

Validated in both directions on the same pair: before the estimate exists, 1 assumed pair and
n_eff 1.54; after, 1 measured pair and n_eff 1.05.

### 2.4 The cap binds the product, not the input

v1 claimed `L_max ≈ 1.5` limited a cluster to 4.5:1 while the contribution was `λ_sig · w_c · LLR`
with `w_c > 1` whenever `n_eff > N_ref`. The cap was checked against the LLR and the contribution
was the scaled product, so it did not hold.

`contribution()` forms the entire product and clamps last, returning both numbers:

```
llr +9.000 × w 1.000 × lambda 4.00 = +36.000 → CAPPED at +1.500
```

`Contribution.capped` makes the clamping visible in the record rather than silent. The independence
weight is separately bounded at 1.0 — belt and braces, because this is the failure that made v1's
stated limit fictional.

The cap itself is derived from the claim's own verification state and cannot be passed in by the
caller, so a weak claim's influence cannot be widened by asking for a larger budget:

| establishes | verified | unverified | disputed |
|---|---|---|---|
| underlying fact | 1.500 | 0.750 | 0.375 |
| only that it was asserted | 0.500 | 0.250 | 0.125 |
| indeterminate | 0.250 | 0.125 | 0.062 |

Note what this does **not** do: it never zeroes a claim that establishes only its own utterance.
Where a contract resolves on whether an authority published an announcement, the announcement *is*
the fact (§7.1) — a blanket zero would discard exactly those cases. Bounded, not silenced. These
numbers are declared policy, not measurements, and are the obvious thing to revisit once there is a
record to fit them against.

---

## 3. The finding that changed the design

**Lexical similarity finds duplication, not events — and no threshold fixes it.**

The pipeline as sketched assumed items could be grouped into event clusters by text similarity.
Measured on this project's own fixture, two independent reports of one committee vote shared two
content words out of thirty-five:

| measure | value |
|---|---|
| Jaccard over content tokens | **0.061** |
| containment, `|A∩B| / min(|A|,|B|)` | 0.118 |
| 5-shingle Jaccard | 0.000 |

There is no threshold that separates that from unrelated text. And the reason is not a weakness of
the fixture: **independent reporting of the same event is lexically unrelated, because that is what
makes it independent.** A clustering rule tuned to merge those two would merge most of the corpus.

So clustering is now **anchor-first**. Items are retrieved *for* a registered proposition, so the
anchor is known at ingest and never has to be inferred; lexical similarity is used for the job it is
actually good at, collapsing syndicated and near-copied text inside a cluster. Items with no anchor
fall back to lexical linkage and will under-merge — the safe direction, since an item wrongly left
out is evidence not used, while one wrongly merged in is evidence miscounted.

This is a correction to the retrieval architecture, not a parameter change: signal collection is
**query-driven from the contract registry**, not a firehose that gets sorted afterwards.

### A second, smaller one: originators need attribution

§7.2 rejects the publication-lag heuristic — a late report may be independent confirmation rather
than a copy. So `is_originator` is set only from explicit attribution. When nothing in a cluster
cites anything, the cluster has **no** originator and says so, rather than nominating the first
arrival. Tested directly: strip the attributions and the originator list goes empty, not to the
earliest item.

---

## 4. Two schema defects the integration found

Both were found by running evidence into a forecast, not by testing the module alone.

1. **`claim_contract_effects` was keyed without `horizon_days`.** The effect is defined per (claim,
   contract, horizon, model) precisely because one fact moves "floor vote this month" and "law this
   year" differently — and the unique constraint omitted the horizon, so the two collided. Retrieval
   had the matching bug: `usable_contributions()` is now horizon-scoped, because summing a claim's
   fourteen-day and one-year effects into one forecast double-counts the claim *and* mixes two
   incompatible questions.

2. **`estimator` was missing from the same key.** A fitted estimate and an LLM's proposed ratio for
   the same quantity are different objects, and holding both side by side is how the proposal gets
   checked rather than trusted. With `estimator` outside the key they could not coexist at all.

---

## 5. Contradictions

Recorded, never auto-applied (§7.3). Automatic downweighting on contradiction is an attack surface:
anyone able to publish a denial could suppress evidence they dislike. Both claims stand; the
contradiction is typed (`genuine_contradiction`, `changed_circumstances`, `scope_mismatch`,
`unresolved`); adjudication requires a named adjudicator and a written reason, and is refused
without both.

---

## 6. What this does not establish

- **Every number in §2.4's cap table is declared, not measured.** They encode a judgement about how
  much a claim in a given verification state should be allowed to move a forecast. Nothing yet tests
  whether those levels are right.
- **`ASSUMED_CORRELATION = 0.30` is a policy floor**, chosen to be non-zero rather than to be
  correct. The whole point of `basis='assumed'` is that it is visible and replaceable by measurement.
- **No LLR here was fitted.** §6 prefers a regularised event-family model with coefficients estimated
  out of sample; what exists is the plumbing that such a model would feed, plus the typing that stops
  an unfitted number entering silently.
- **No real signal has been ingested.** Every item is a fixture. The RSS/Atom collectors in
  `docs/DATA-SOURCES.md` are reachable only from a host with network access.

---

## 7. Next

1. **Point the collector at real feeds** — the anchor-first architecture means this is a query per
   registered proposition, not a firehose.
2. **Fit the first event-family model** so `estimator='fitted_model'` means something measured.
   Blocked on having resolved cases, i.e. on Phase 3.
3. **Replace assumed correlations with observed error agreement** as the record accumulates. This is
   the quantity the whole n_eff calculation rests on and it is currently mostly assumption.

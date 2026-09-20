# SPINE v2 — Design and Roadmap

**Supersedes** `SPINE-DESIGN-FINAL.md` and `SPINE-SIGNALS.md`, which are retained as history.
**Rewritten** 19 Sep 2026 following an external design review that invalidated three load-bearing
parts of v1. Disposition of all 27 findings: `REVIEW-RESPONSE.md`.

**Nature.** A design document with implementation underway. `db/schema_v2.sql` (36/36), the `spine/`
ledger (33/33), the scoring and decision layers (67/67) and the models and ablation harness (43/43)
are real and tested; the rest is specification.
No forecast has been made, nothing has been benchmarked, and no accuracy or profitability advantage
has been demonstrated.

**Phase status:** 0 blocked on network access only · **1 PASSED** · **2 PASSED ON FIXTURES** ·
**4 BUILT** · 3 blocked on live data and calendar time. The §6–§7 evidence pipeline, §10.2–§10.3
shadow execution, the evaluation loop and the scheduled collector are all built and joined end to
end (`tests/test_e2e.py`). **What remains blocked is live market data and four human decisions, not
code.** 777 checks across 15 suites (`python3 run_tests.py`), plus 30 documentation-consistency
checks (`--docs`). Operating instructions: `docs/RUNNING.md`.

**Posture: paper only.** Operations are UK-based, where Polymarket is close-only on both frontend
and API. Live trading is **out of scope** — see §2.1. Everything through Phase 3 is unaffected.

---

## 0. What changed from v1, and why it needed a rewrite rather than patches

Three failures were structural:

1. **The probability firewall was wrong.** It capped *the probability of an event* by horizon class,
   confusing that with *confidence in the estimate*. A ten-day event can legitimately be 3% or 97%.
   Worse, an earlier revision "fixed" a one-tailed cap by making it symmetric — which made the
   database reject a legitimate 2% forecast outright. Verified live against the v1 schema.
2. **There was no evidence-to-probability mapping.** A likelihood ratio was stored per news cluster,
   unbound to any contract or horizon. One fact has different predictive weight for different
   contracts; a context-free LLR is not a quantity.
3. **The validation arithmetic was used more confidently than its assumptions justified** — wrong
   power coefficient, an unexamined assumption that weekly effective sample sizes add, and a
   variance substitution that made a convenient property look like a result.

Patching these section by section would have left a document whose parts disagreed. v2 is
restructured around the frame the review proposed, which is better than what it replaced.

---

## 1. The organising frame: three propositions, tested separately

> **A verified fact is not necessarily predictive information.
> Predictive information is not necessarily an opportunity at the available price.**

v1 collapsed these into one pipeline that counted verified signals and converted them into
confidence. v2 keeps them apart, because each can fail independently and each needs its own evidence:

| Proposition | Claim under test | Fails when |
|---|---|---|
| **P1 — Evidence** | We can extract, verify and attribute claims without fabricating corroboration | Extraction is lossy; syndication inflates apparent independence; corrections are missed |
| **P2 — Prediction** | Those claims shift a precisely defined outcome probability, beyond what the price already reflects | The fact is real but priced in, or irrelevant to the resolution rule |
| **P3 — Opportunity** | That improvement survives uncertainty, execution and costs | Edge is real but smaller than the spread, or capacity is negligible |

P3 is answered under the §2.1 paper-only posture as a **research question** — did the edge survive
simulated execution against recorded books — not as a trading outcome.

Each has its own evaluation track (§12). A system can pass P1 and P2 and still be worthless.

---

## 2. Premise and eligibility

### 2.1 Gate zero — jurisdiction: RESOLVED, paper only

**Declared 19 Sep 2026: operations are UK-based.** Polymarket lists the UK as close-only on both
frontend and API — existing positions can be exited, none opened — as it holds no Gambling
Commission licence.

**This does not block the work.** Paper trading reads public market data and simulates fills against
recorded books. No account, no wallet, no order placement, no KYC. Phases 0–4 are entirely
unaffected, and shadow execution — the thing that answers P3 — works exactly as designed, because
simulating a fill needs the book, not permission to trade.

**What it does change is permanent, not temporary.** Live trading is **out of scope**, and it
re-enters scope only through a change of circumstance — a licence Polymarket does not currently
hold, or relocating operations — never by passing a gate in this document. So:

- `order_manager` and the live-risk interlock (halt semantics, expiring health lease, heartbeat
  auto-cancel, order reconciliation) are **not built**. This removes the most dangerous code in the
  design, and the review that prompted v2 was right to be most worried about exactly that surface.
- The decision layer becomes a **research instrument**: it measures whether an edge *would have*
  survived execution and costs, which is P3 as a scientific question rather than an operational one.
- Success means "we established whether this works", not "we made money." That is a smaller claim,
  and it is the one the validation machinery in §3 and §12 was actually built to support.

**Not legal advice.** The factual position above is from Polymarket's published geoblock policy. If
live trading is ever contemplated, UK gambling and financial regulation around prediction markets
needs proper advice rather than a line in a design document.

### 2.2 Scope, stated once

v1 contained a genuine contradiction: its Phase 0 deleted the trading subsystem unless enough
*long-dated structural* markets existed, while its addendum traded *short-horizon* T1/T2 and kept
T3 forecast-only. Both cannot govern. v2 resolves it in favour of the addendum:

| Tier | Horizon | Action | What it can validate |
|---|---|---|---|
| **T1** | 7–14 days | Paper only | The short-horizon model only |
| **T2** | 1–6 months | Paper only | The mid-horizon model only |
| **T3** | 6 mo – 2 yr | Publish only | Nothing, on any human timescale |

Under the §2.1 posture every tier is paper. The order-management service is not built at all, so
"trading is a rebuild, not a flag" understates it: there is nothing to flag.

**The cost, stated plainly:** these are three models, not one model at three horizons. T1 does not
use structural pressure vectors — five-year debt trajectories say nothing about a court ruling next
Thursday. **Calibration earned at T1 transfers to T3 not at all.** Every published T3 forecast
carries that disclosure.

---

## 3. What can and cannot be established

### 3.1 The power approximation, corrected

v1 published `n_eff ≳ 24.7 / BSS` and treated it as a feasibility bound. Two corrections:

**The coefficient was wrong for the stated gate.** It was derived with a one-sided α = 0.05
(z = 1.645) while the gate uses the 2.5th bootstrap percentile, i.e. one-sided 2.5% (z = 1.96).
Consistent with the gate actually specified:

```
n_eff ≳ (1.96 + 0.8416)² × 4 / BSS = 31.4 / BSS
```

Every v1 figure was ~27% too low. At BSS = 0.10 the requirement is ~314, not 247.

**It is an approximation, not a law.** The exact variance is

```
Var(d) = 4·E[δ²·p_m(1−p_m)] + Var(δ²)
```

v1 substituted benchmark variance `p_b(1−p_b)` for the model-conditional term and dropped the
second entirely. The "base-rate independence" v1 highlighted is an artifact of that substitution.
**Treat the table as an illustrative scenario. Establish the real requirement by simulating the
actual question mix, temporal dependence and decision rule.**

**And a logic correction:** v1 said an underpowered gate means "any pass would be noise." Low power
means a low probability of *detecting* an effect of the specified size; it does not make a result
that fires noise. The practical conclusion — `n_eff = 10` is not a usable gate — survives, and
strengthens under the corrected coefficient.

### 3.2 Weekly effective sample sizes do not add — measured in Phase 1

v1 assumed they accumulate linearly. Phase 1 simulated it
(`phase1/serial_dependence.py`, full results in `docs/PHASE1-REPORT.md`) and the answer is worse
than "they don't quite".

**Effective sample size has a ceiling that time cannot lift.** A component `g` shared across *all*
observations — one analyst, one frozen model version, one macro regime — makes `n_eff → 1/r_between`:

| `r_between` | n_eff ceiling | @1yr | @5yr | weeks to n_eff = 314 |
|---|---|---|---|---|
| 0.0000 | unbounded | 308 | 1541 | 53 |
| 0.0032 | 312 | 157 | 261 | **unreachable** |
| 0.0500 | 20 | 19 | 20 | **unreachable** |

**Between-week correlation must stay below 0.0032** for the Phase 3 gate to be reachable at all.

**And the gate as specified is invalid, not merely slow.** Simulating the decision rule — cluster
bootstrap over weeks, 2.5th percentile — against a *true effect of zero*:

| `r_between` | weeks | fires on noise |
|---|---|---|
| 0.000 | 52 | 2.5% (nominal) |
| 0.005 | 52 | **15.0%** |
| 0.050 | 52 | **37.0%** |
| 0.050 | 260 | **38.0%** |

Running five times longer does not help. The mechanism: the grand mean is **not consistent** when a
global component exists — its variance tends to `γ²`, not zero — while a week-level bootstrap cannot
see `g`, because every resample contains it, and keeps shrinking the interval. At 1040 weeks the
reported interval is **10× too narrow**.

**The fix is to sample `g` rather than bootstrap around it**: rotate whatever it is constant within
and bootstrap at that level.

| independent regimes | false-positive rate | power |
|---|---|---|
| 1 | 43.5% | 82% |
| 4 | 13.0% | 66% |
| **12** | **4.0%** (nominal) | **94%** |

**At least ~12 independent regimes** are required. One regime is worthless at any duration. Forecasts
therefore carry a mandatory `regime_id`, and scoring groups by it.

This cuts against the instinct to freeze everything for reproducibility. Both are needed: freeze
*within* a regime so each forecast reconstructs, rotate *across* regimes so `g` is sampled.

`r_between` is now a **measured deliverable** — Phase 3 estimates it from the accumulating record
and re-derives its own stopping rule. The simulation bounds the problem; it does not measure the
real value, which only resolved forecasts can supply.

### 3.3 Basket size — what survives and what does not

v1 claimed `n_eff` is non-monotonic in weekly basket size, so the basket should equal the cluster
count. That holds **for the equally-weighted estimator v1 specified**, and not in general: a
sensibly weighted analysis can downweight correlated observations rather than be diluted by them,
and additional observations then weakly *increase* information.

So the correct conclusions are narrower: **replace the equally-weighted cluster bootstrap with a
weighted estimator**, after which "one market per cluster" is a workload heuristic rather than a
statistical optimum. It was stated as the latter on the strength of a 20-market synthetic fixture.

### 3.4 Skill is not calibration

A positive Brier Skill Score can come entirely from resolution while reliability stays poor. v1
claimed passing the gate "demonstrates that the T1 model is calibrated." It demonstrates skill.
Calibration is the reliability term, reported separately, by family and horizon.

---

## 4. Architecture

Four layers with explicit interfaces, mapping onto the three propositions.

| Layer | Owns | Proposition |
|---|---|---|
| **Contract registry** | Exact target, rules version, deadline+tz, payout states, eligibility, resolution history | P3 boundary |
| **Evidence ledger** | Artifacts, claims, lineage, contradictions, corrections, availability times | P1 |
| **Forecasting** | Baseline, independent forecast, market-conditioned forecast, consistency constraints | P2 |
| **Decision & risk** | Executable price, costs, uncertainty, exposure, eligibility — most often outputting *no trade* | P3 |

Services run one process per container, `tini` as PID 1, `exec`-ed so SIGTERM arrives directly.
SQLite in WAL with `busy_timeout`; the data volume must be local disk.

**Start small.** Do not stand up a feature store, orchestrator and vector index at the outset. Build
the smallest system that demonstrably preserves availability-time correctness and lineage; add
infrastructure when its operational benefit is concrete, never as a proxy for rigour.

---

## 5. The probability model

### 5.1 Three things, never conflated

v1's firewall is removed. In its place:

| Quantity | Meaning | Constraint |
|---|---|---|
| **Estimate** | `p_est` — best estimate of the outcome | `0 < p < 1`. **Nothing else.** |
| **Uncertainty** | `[p_lo, p_hi]` with a named method | Must contain the estimate; width ≥ a horizon-class minimum |
| **Permission** | Whether to act, and how much | Exposure caps; abstention requires a reason |

**Cap exposure, not the probability of reality.** "Estimated 8%, insufficient evidence to trade" is
a coherent output. "We cannot say less than 40% because this is T1" is not.

The discipline the firewall was reaching for survives, relocated: horizon class sets a **minimum
interval width** (short-horizon contingent claims must carry wider uncertainty) and **exposure
caps**. Neither touches the estimate.

Also corrected: a SQL `CHECK` **rejects a row**; it never clamps a value. v1's prose claimed
clamping throughout and the schema never did it.

### 5.2 Two forecasts, not one

- **Independent forecast** — produced without seeing the target price. Measures what the evidence
  system knows on its own.
- **Market-conditioned forecast** — tests whether the evidence adds anything beyond the
  contemporaneous price:

```
logit(p_{m,t}) = a_g + b_g · logit(q_{m,t}) + f_g(x_t, h)
```

for event family `g`, remaining horizon `h`, with `f_g` strongly regularised. This is a model class
to be tested, not a source of edge. Its purpose is to stop the system repeatedly adding publicly
known facts to a prior the price already reflects.

### 5.3 Reference classes

`p₀ = (k + α)/(n + α + β)`, with **α and β fitted per family** from that family's historical base
rate. Beta(1,3) has prior mean 0.25 and is not a rare-event default; v1 applied it universally.

More consequentially, "will X happen by date D" is a **hazard problem, not a static probability**.
Each class must define its exposure window, eligible population, censoring, and the treatment of
elapsed time. Three horizon tiers do not remove the need for event-family models: a court ruling, a
data release and an election are not the same statistical problem because they resolve in ten days.

**And the exposure denominator has to be stored, which it was not.** The hazard is
`(k + α)/(exposure + α + β)`, so a forecast made this way cannot be rebuilt from the record without
the exposure it divided by. That number lived only in the Python dataclass: a reconstruction would
have found `n`, `k`, `α`, `β` and nothing else, and produced the **static rate** — the estimator
this section exists to reject — while appearing to succeed. On the fixture the two differ by 264bp.
`exposure_units` and a name for the unit are now columns, returned by `ledger.reconstruct()`, and
`tests/test_refclass.py` rebuilds a hazard forecast from stored inputs alone and checks it to the
basis point.

`spine/refclass.py` is the writer these tables never had. Four things it refuses: **counts supplied
by the caller** (`k` and `n` are derived from the roster, because two sources of the same number
eventually disagree); **members that postdate the freeze** (a class frozen before the cases it
contains is a class selected on outcomes); **an unnamed exposure unit** (forty case-weeks and forty
case-years are the same `n` and a tenfold difference in rate); and **a selection rule referenced by
name** — the rule is content-addressed through a manifest, because §9.2's objection to committing to
labels applies here exactly as it does to model versions.

### 5.4 Conditional probability, corrected

v1 stated `P(B) = P(B|A)·P(A)`, valid only when B cannot occur without A. Generally:

```
P(B) = P(B|A)·P(A) + P(B|¬A)·P(¬A)
```

### 5.5 The coordination penalty is a subjective prior

`p ← p·ρ^(N−1)` with ρ ≈ 0.7 does not follow from having done an incentive analysis. It is a
made-up functional form with a made-up constant. It is retained **only** as an explicitly labelled
subjective assumption with mandatory sensitivity testing, or replaced by explicit conditional
scenarios whose effects are estimated.

---

## 6. Evidence → contract effects

**The missing core of v1.** A likelihood ratio is meaningless without a target:

```
LLR_{c,m,t} = log [ P(E_c | Y_m = 1, I_{t⁻}) / P(E_c | Y_m = 0, I_{t⁻}) ]
```

`m` identifies the exact contract; `I_{t⁻}` is the information already incorporated. A bill clearing
committee strongly affects "floor vote this month", weakly affects "law this year", and barely
touches "enacted by tomorrow". There is no one number.

**Claims and effects are separate objects.** A claim records what was asserted, by whom, when, with
what support and verification status. A *claim-to-contract effect* records which contract it moves,
over what horizon, under which model and parameter version, conditioned on what.

**Prefer a fitted event-family model to hand-assigned ratios** — a regularised logistic combining
procedural state, time remaining, observations and market information, with coefficients estimated
and evaluated out of sample. An LLM can extract structured observations and propose causal
relevance; **an LLM-generated "likelihood ratio of 3.2" is not a measurement** and is typed as
`llm_proposed_unvalidated` in the schema so it cannot silently become an input.

**The influence budget binds the final contribution**, including every scaling factor. v1 claimed
`L_max ≈ 1.5` capped a cluster at 4.5:1, but the contribution was `λ_sig · w_c · LLR_c` with
`w_c > 1` whenever `n_eff_sources > N_ref`. The cap did not hold. The schema now enforces
`|final_contribution| ≤ contribution_cap` directly, and `spine/evidence.contribution()` forms the
whole product before clamping, returning both the raw and the final value so the clamping is
visible rather than silent.

The **cap is derived from the claim's own verification state** and cannot be supplied by the caller,
so a weak claim's influence cannot be widened by requesting a larger budget. It bounds exposure to a
claim and never zeroes one: where a contract resolves on whether an authority published an
announcement, the announcement *is* the fact (§7.1), and a blanket zero would discard exactly those
cases.

An effect is keyed per **(claim, contract, horizon, estimator, model version, vintage)**. Retrieval
is horizon-scoped: summing a claim's fourteen-day and one-year effects into one forecast
double-counts the claim and mixes two incompatible questions.

---

## 7. Verification

### 7.1 Four questions, answered separately

Source binding is not truth verification. Each claim records:

1. **Authenticity** — is this really the filing/statement/dataset?
2. **Faithful extraction** — does the claim preserve negation, qualification, scope, date?
3. **What it establishes** — the underlying fact, or only that someone asserted it?
4. **Contract relevance** — does the fact affect the *resolution condition*?

An authentic statement that "negotiations are progressing" establishes only that the statement was
issued. Conversely, where a contract resolves on whether a specific authority publishes a specific
announcement, the authentic announcement may establish the condition directly. **Treatment depends
on the claim and the contract, never on a blanket source label.**

### 7.2 Lineage, not source counting

Co-publication measures *coverage overlap*, not *error independence*. Two outlets with their own
correspondents at the same scheduled event co-occur constantly with independent errors; two
unrelated outlets relying on one anonymous source have correlated errors and no co-publication
signal. A late report may be independent confirmation, not a copy — so v1's publication-lag
heuristic would wrongly discount it.

Build a provenance graph: **artifact → extracted claim → cited upstream evidence → reporting origin
→ contract.** Use textual overlap, explicit attribution, timestamps and reporting descriptions to
identify shared evidence. Ownership and co-publication are clues, not determinations.

**Correction from implementation (`docs/EVIDENCE-REPORT.md` §3): textual overlap groups duplicates,
never events.** Measured here, two independent reports of one committee vote shared two content
words out of thirty-five — Jaccard 0.061, containment 0.118, 5-shingle overlap 0.000. No threshold
separates that from unrelated text, and the reason is structural: independent reporting of the same
event is lexically unrelated, which is what makes it independent.

Signal collection is therefore **query-driven from the contract registry** — items are retrieved
*for* a registered proposition, so the event anchor is known at ingest and never inferred.
Implemented in `spine/collect.py`: queries are **predeclared and stamped**, like a reference class,
because one written after seeing which articles would have helped is a selection rule fitted to the
outcome. Which query retrieved an item is stored as provenance, so the anchor is checkable rather
than asserted, and a failed fetch is recorded as a run — a gap that looks like "no news that day" is
indistinguishable from evidence of quiet. Text
similarity is retained for the job it actually does: collapsing syndicated and near-copied
artifacts. Unanchored items fall back to lexical linkage and will under-merge, which is the safe
direction: an item wrongly left out is evidence unused, while one wrongly merged in is evidence
miscounted.

An unmeasured source pair is **assumed correlated at a declared floor, never independent**. Assuming
independence is the flattering direction and must not be the default; `n_eff` is additionally
clamped at the headcount, because an estimate should never claim more independent sources than
there are sources.

`n_eff_sources` is retained as a summary but is now computed from *error-correlation* estimates with
an explicit `basis` field recording how each was derived, including `assumed`.

### 7.3 Contradictions are recorded, not auto-applied

Automatically downweighting on contradiction lets an unsupported denial dilute strong evidence —
an attack surface. Preserve both claims, distinguish changed circumstances from genuine
contradiction, and adjudicate support asymmetrically with the decision logged.

**The same rule applies to outcomes, and originally did not.** `resolutions` was keyed
`UNIQUE(proposition_id)`, so correcting a disputed outcome meant deleting the original and erasing
the fact that it was ever in doubt. Resolutions are now **revisions**: append-only, undeletable,
each correction carrying a written reason and a named adjudicator. Scores record which revision they
were computed against, so a correction adds a score rather than invalidating one.

---

## 8. Point-in-time correctness

### 8.1 Five times, one retrieval key

Arrival time is not availability time. A report arrives 10:00, extraction finishes 10:02,
verification 10:05 — a decision simulated at 10:01 must not see the verified signal. v1 keyed
everything on `first_seen_at`, which is wrong.

**event_at · claimed_published_at · first_seen_at · artifact_created_at · available_for_decision_at**

Only the last governs retrieval, and it applies to *derived* features too — cluster assignments and
source-reliability estimates included. Source reliability learned from outcomes and applied
retroactively leaks outcome information backwards; scores are themselves point-in-time.

**And every one of those retrievals is a string comparison, so the format is part of the
correctness.** `datetime.isoformat()` omits the fractional part on a whole second, and `'.'` (0x2E)
sorts before `'Z'` (0x5A), so `'2026-09-19T13:00:00Z'` compares **greater** than
`'2026-09-19T13:00:00.000Z'` — the same instant. A forecast written the first way was invisible to a
decision made at that instant: wrong by a format rather than by an interval, silently, and only for
the rows that landed on a round number.

The canonical form is `YYYY-MM-DDTHH:MM:SS.sssZ`, produced by `spine/timeutil.py`, enforced by
`CHECK ... GLOB` on every column that is compared. Timestamps that are *merely recorded* — a
source's `claimed_published_at`, a venue's own stamp — are left free-form on purpose: those are
evidence about the outside world, and normalising them would be rewriting what the source said.
`register_forecast()` **validates rather than canonicalises**, because `created_at` is
hash-committed and quietly reshaping a committed field would change what the chain attests to.

### 8.2 Feast does not do this for you

v1 said Feast "implements point-in-time joins directly — do not hand-roll it." Overstated. Feast's own
documentation notes that by default it does **not** compare the created timestamp against the entity
row's event timestamp, so a correction or backfill created *after* the lookup time can still be
returned. `created_timestamp_column` must be configured explicitly and semantics vary by offline
store. Use it, pin the version, and test the backend.

**Mandatory regression test:** a value observed at 10:00 and corrected at 11:00 — a forecast
reconstructed for 10:30 still receives the original value.

### 8.3 Split training by label availability

The fit/validation/test date split is insufficient alone for overlapping multi-year forecasts: an
example created before a cutoff may resolve after it. Require `label_available_at` to precede the
cutoff and purge overlapping examples. Fit normalisers, feature selection, source scores and
calibration inside the same temporal discipline.

### 8.4 There are more than two free parameters

v1 named λ and λ_sig. Also tunable: reference-class selection, feature weights, ρ, clustering
thresholds, source weights, market-selection rules. **All are researcher degrees of freedom and all
belong in the governance record.**

---

## 9. Data model

`db/schema_v2.sql` — 20 STRICT tables. Probe suite `db/test_schema_v2.py`, **36/36 passing**,
re-running every finding the review demonstrated against v1.

What the schema now enforces that v1 only claimed:

| Control | v1 | v2 |
|---|---|---|
| Non-integer probability | accepted | rejected (STRICT) |
| Out-of-range base probability | accepted | rejected |
| Mutating a frozen reference class | accepted | rejected; classes are immutable versions |
| Mutating a stored claim/effect | accepted | rejected; append-only |
| DAG cycles | accepted | rejected via recursive-CTE trigger (diamonds still allowed) |
| `sources` table | undefined | defined |
| Legitimate p = 0.02 at T1 | **rejected** | accepted |
| Trading while close-only | unmodelled | rejected |
| UMA 50/50 payout | lost | recorded alongside the void outcome |

### 9.1 Pre-registration needs an external anchor

A hash chain proves internal consistency and detects tampering *within* a chain. It does not prevent
fabricating an entire chain later with backdated timestamps — and pre-registration is this system's
core credibility claim. The chain head must be periodically anchored in an independently timestamped
append-only destination (RFC 3161, or a public log). `chain_anchors` records it.

### 9.2 Commit to inputs, not their names

`lambda_version` is a label; changing what it points to changes reconstructibility without changing
the forecast hash. Every forecast references a **content-addressed manifest** enumerating the data
snapshot, evidence, model config, source-reliability version and market rules by hash. For
LLM-assisted forecasts that includes the actual prompts, retrieved context, tool results and raw
output — not version labels.

### 9.3 A freeze timestamp proves almost nothing

`frozen_at < created_at` is necessary and wildly insufficient for "the analyst had not seen the
case." That needs a procedural control: predeclared family-level selection rules, logged decisions,
and an auditable separation between defining a class and applying it. `selection_rule_ref` points at
the predeclared rule; the timestamp alone is not the control.

---

## 10. Decision and execution

### 10.1 Forecast the contract

Bind to the exact outcome token, rules version, deadline with timezone, resolution source and
clarification history. For research, preserving the original question is right. **For trading, a
material mismatch blocks new exposure until reviewed** — enforced, not merely recorded.

**Payout is not binary.** Rules govern resolution, disputes escalate through UMA, and an
"Unknown/50-50" outcome can pay $0.50 per token. Settlement state and payout are recorded separately
from the binary research outcome, so a position cannot vanish from economic evaluation because its
outcome was excluded from Brier scoring.

**A rules amendment is a new contract, not an edit.** Polymarket amends resolution text after
listing. `spine/registry.py` makes the rules-text hash part of the contract's identity, so an
amendment inserts a new row and every standing forecast keeps pointing at the text it was made
against. `divergence_check()` reports the difference and mutates nothing: auto-disqualifying a
forecast because someone appended a clarification is the over-correction §5.1 warns about.

**A market with no deadline or no rules text is refused registration, with a reason recorded.** A
contract whose settlement rule is unknown cannot be forecast against, and a placeholder rule is
worse than no contract.

**Eligibility is stamped with a check time.** It is a fact about a jurisdiction on a date, not a
permanent property of a market.

### 10.2 Evaluate the price you could actually get

A midpoint is not an executable price.

```
EV_per_share = p_settlement − expected_acquisition_price − additional_costs
```

Expected acquisition price **at the intended size**, including book depth. Do not subtract the
spread twice. Compute EV against the conservative end of the uncertainty interval, not the point
estimate:

> Central 64%. Conservative 60%. Executable 61¢. Costs 1¢. **EV −2¢ → abstain.**

That is a more useful output than "verified signals favour YES." Fees must be retrieved per market —
categories differ and a blanket assumption is wrong.

For passive orders, "the market touched my limit" is not a fill: model queue position, partial
fills, cancellation latency, and the fact that fills arrive disproportionately when the price is
moving against you.

Implemented in `spine/shadow.py`. Queue position is FIFO against the depth resting at the limit, and
the traded volume that clears it is a **required input from a trade feed, never inferred from depth
changes** — cancellations and fills are indistinguishable in depth data, so a book that thins out
because everyone pulled would otherwise score as a full fill. Cancellation latency fills are
flagged rather than assumed away. Adverse selection is **measured** by marking each fill out against
a later snapshot, so §10.2's claim about when fills arrive becomes a finding on this project's own
record rather than an assumption baked into the model.

**Paper and live are different kinds of record.** `trade_decisions.mode` is `NOT NULL` with no
default, and the eligibility invariant is stated on live permissions alone:

```sql
CHECK (permitted = 0 OR mode = 'paper' OR eligibility_status = 'tradeable')
```

The earlier form — `permitted = 0 OR eligibility_status = 'tradeable'` — was right about live
trading and wrong about what it was checking. Under §2.1's close-only posture it made *every* paper
decision storable only as a refusal, destroying the counterfactual the paper run exists to produce.
A default of `'paper'` was rejected for the opposite reason: it would silently relabel an omitted
live decision as a simulation. `spine/decision.record()` is the only writer to the table, so the
column cannot be omitted in practice.

### 10.3 Shadow execution starts on day one

v1 put paper trading after a 10–18 month accuracy gate. If execution eats the edge you learn that at
month eighteen. Order-book collection and shadow fills run **in parallel with prospective
forecasting from the start**. It costs almost nothing and it can end the project early and cheaply.

`book_snapshots` carries the same three-timestamp discipline as `signal_items`: the venue's own
timestamp is a claim, `captured_at` is observation, and only `available_for_decision_at` governs
retrieval. A **crossed book is refused** rather than repaired — it means the two sides were read at
different moments, and both spread and midpoint would be fictional at exactly the moments execution
is most expensive. Markouts live in their own table because a number knowable only later must not
look writable at the moment of the fill.

---

## 11. Risk and safety

### 11.1 What "halt" must mean

v1's kill switch stopped new work and said nothing about resting orders. Stopping the strategy does
not cancel them, and switching a process to paper does not remove exposure. Halt means:

> **No new exposure. Cancellation and reconciliation remain active. Monitoring and audit writes
> continue. Existing positions stay under explicit risk management.**

Order entry is gated by an **expiring health lease**, not a persistent "healthy" flag: a dead
monitor cannot clear a flag, but a lease fails safe on its own. Assess the venue's heartbeat
auto-cancel as an additional safeguard, never as a substitute for reconciliation.

### 11.2 Sizing

Position limits derive from **monetary loss scenarios, concentration, liquidity and uncertainty** —
not from `n_eff`. v1 proposed sizing on cluster effective sample size, which conflates statistical
evidence with financial risk.

Cluster-level *concentration* limits survive, because eight markets downstream of one thesis is one
bet economically as well as evidentially — but driven by **loss covariance**, which is a different
quantity from forecast-score correlation. The schema types dependence explicitly as `score`, `loss`
or `evidence`; a single generic correlation field cannot serve all three.

### 11.3 Untrusted input

Retrieved news and documents are untrusted throughout. They must never be able to instruct the
system to execute tools, expose secrets or alter trading controls. **The signer and the risk checks
sit outside any LLM's authority.** v1 did not address this at all.

Implemented in `spine/untrusted.py`. There is no LLM in the pipeline yet, so the prompt-injection
half is not live; the other half already is, because this project ingests open-internet text into a
database and then adjudicates it.

**Display-altering characters get a split treatment, and the split is the point.** A bidirectional
override makes text render in an order other than the one it is stored in, so a claim that hashes
one way reads another way to the human adjudicating it — `RAISE` and `\u202e`+`ESIAR` are different
bytes, the same picture, and one signature. Source text is therefore **preserved verbatim and
flagged**: if a publisher really emitted an override, that is a fact about the publisher and
stripping it destroys evidence. Our own `claims.assertion` is **refused**, because nothing
legitimate needs an invisible reordering control in text we wrote.

**A link is not a document.** `javascript:`, `data:` and `file:` are instructions waiting for
something to follow them. The scheme is checked at ingest, the unsafe value is dropped rather than
stored, and the rejection is recorded — the article is still evidence when its link is not usable.
A stored hazard is a hazard on the day something reads it, not the day it arrives, and this project
will grow a reporting surface.

SQL injection is closed by construction rather than by filtering: a test walks the AST of every
module and asserts that no `execute()` f-string interpolates anything but an internal column list.

---

## 12. Evaluation programme

| Track | Establishes | Fails when |
|---|---|---|
| **Evidence quality (P1)** | Extraction fidelity, entity/time/negation handling, attribution, correction handling, false-corroboration rate | Independence is manufactured |
| **Forecast quality (P2)** | Prospective paired performance vs matched baselines; calibration by family and horizon; coverage; ablations | Edge is priced in |
| **Economic quality (P3)** | Net result under realistic fills, latency, costs, concentration, drawdown, capacity, settlement delay | Edge is real but unreachable |

**Compare the same contracts at the same information cutoffs.** Score a predefined eligible sample
*including no-bet decisions*, then report the trading subset separately — calibration on a
self-selected subset is meaningful only for that subset.

**Predeclare evaluation dates or use a justified sequential-testing procedure** —
implemented in `spine/sequential.py` and measured, not merely asserted. Checking a fixed 95% bound
weekly for a year inflates the false-positive rate roughly six-fold, to **19.3%**, with no bad faith
required:
the gate is cheap, data accumulates, and someone looks. Alpha spending restores it —
O'Brien-Fleming gives 1.1% at 52 looks while keeping full power — and is the default because a false
"it works" costs far more here than running the full schedule. **The number of looks is declared
before the record starts**, since choosing it afterwards restores the freedom the schedule removes.

**And this is now enforced, because the readout was committing the error itself.** The first
`evaluate()` reported a gate verdict from a plain 95% bootstrap interval — fine once, and not fine
on a schedule, which is exactly how `run_cycle.py` is meant to run. Every run was a look. The
machinery to prevent it had existed since Phase 2 and nothing was wired to it.

The gate now takes its threshold from a declared plan (`evaluation_plans`, immutable and
undeletable, naming who declared it) and every look is recorded (`evaluation_looks`, append-only,
capped at the declared count by trigger). Looks are free until recorded; recording one is a
deliberate and irreversible act. **The gate does not evaluate at all for a slice with no plan** — so
the number of looks cannot be chosen after seeing the data, because nothing can be seen until it is
chosen.

On one record, the whole argument: a fixed 95% lower bound of +0.1157 would have fired; look 1 of 10
under O'Brien-Fleming demands z = 6.09 and does not. Thresholds relax across the schedule (6.09,
4.23, 3.40, 2.95, 2.68, 2.52, 2.42, 2.35, 2.31, 2.28), the last still stricter than a nominal
one-sided 1.645.

**Ablations are the point.** Does adding verified signals improve anything? Does deduplication help?
Does lineage weighting help? Does the evidence model beat the contemporaneous price? Without these
you can build an elaborate pipeline and never learn which parts contribute.

External benchmarks provide context, not a passmark — question sets differ, which is why they
publish difficulty adjustments. The primary benchmark stays matched to our own contracts and
decision times.

---

## 13. Roadmap

Timelines below are **provisional pending §3.2** — if between-week dependence is material they
stretch by up to an order of magnitude. That measurement is itself Phase 1 work.

| Phase | Work | Gate |
|---|---|---|
| **0 · Feasibility** | Jurisdiction/eligibility (first). Market screen with corrected coefficient. Vintage audit. Throughput pilot. | Eligibility known; enough independent clusters; vintage coverage adequate; throughput consistent with §3.1 |
| **1 · Ledger & registry** ✅ | Contract registry, evidence ledger, availability-time discipline, schema v2 live, external anchoring. Measure serial dependence. | **PASSED** — `tests/test_phase1.py`, `db/test_schema_v2.py`. See `docs/PHASE1-REPORT.md`. |
| **2 · Narrow forecasting** ✅ | One or two event families. Baseline + independent + market-conditioned. Frozen selection rules, explicit abstention. **Shadow execution runs in parallel.** | **PASSED ON FIXTURES** — models, ablations, registry, evidence pipeline and shadow execution all built and joined. The event-family *selection* is a human decision still owed. See `docs/PHASE2-REPORT.md`. |
| **3 · Prospective evaluation** | Accumulate pre-registered forecasts across **≥12 independent regimes**. Estimate `r_between` from the record and re-derive the stopping rule. | **BLOCKED on live data and calendar time, not code.** The machinery is built and validated (`spine/evaluate.py`): the gate refuses to evaluate without a declared look budget, below twelve regimes, or with scores left stale by a resolution revision |
| **4 · Container** ✅ | Multi-stage CPU-only build, tini, health lease, restart safety. | **BUILT** — `Dockerfile`, `serve.py`, `tests/test_serve.py`. Clean SIGTERM (measured at 0.2s on a 30s interval); restart re-registers nothing. **"File secrets" and "cancels open orders" are struck**: under §2.1 there are no orders to cancel and no credential to mount, and a secrets facility with no use is a liability the moment one appears |


**Phase 0 can end the project. So can shadow execution**, and that is the point of running it
alongside forecasting from Phase 2 rather than at the end. Under the paper-only posture Phase 5 is
the terminal phase: it answers P3 and the programme concludes with a finding, not a position.

### What the code is waiting on

Every remaining blocker is data or a decision. In order of how cheaply they resolve:

1. **Run `python3 run_cycle.py --check` from a networked host.** Both venue endpoints are public and
   need no key; the build sandbox simply has no route to them.
2. **Declare the evaluation budget** (`evaluate.declare_plan`). The gate refuses to evaluate without
   it — deliberately, since choosing the number of looks after seeing the data is the failure alpha
   spending exists to prevent.
3. **Name the regime rotation** — what actually varies across the twelve regimes. This decides
   whether the bootstrap means anything at all.
4. **Choose one or two event families** and freeze their selection rules.
5. **Set `min_width_bp` per horizon class**, and revisit the influence caps in `spine/evidence.py`.
   Both are currently declared policy rather than anything measured.

---

## 13b. Data sources and access

**Constraint: free and open-source only.** Full table with verified access terms in
`docs/DATA-SOURCES.md`.

Implemented in `spine/venue.py` and driven by `run_cycle.py`, which screens the universe, registers
contracts and records books in one command. The client has **no authentication path at all** — no
header, no key handling, no signing — and a test asserts it, so the paper-only posture is a property
of the code rather than a promise about how it is used.

The material point for the roadmap: **Phase 0's critical path requires no registration whatsoever.**
The Polymarket Gamma API and the CLOB *read* endpoints are public — no key, no account, no wallet —
so the market screen, the contract registry and the order-book snapshots that shadow execution needs
are all reachable with nothing but network access. Authentication is required only for order
placement, which is a Phase 6 concern behind the jurisdiction gate.

Free keys are needed later for FRED/ALFRED, CourtListener and congress.gov. None blocks Phase 0, and
FRED is lower priority than §13 implies: Stage 2 pressure vectors feed **T3**, the tier that cannot
be validated on any human timescale, while T1 and T2 do not use them.

The constraint drops NewsGuard and paid news APIs. That is survivable — §7.2 already required source
independence be learned from observed behaviour rather than bought as a vendor score.

---

## 13c. What this project is taking on faith

Every tunable number is registered in `spine/params.py` with its provenance, and
`tests/test_params.py` refuses to let an unregistered constant exist in `spine/`. Four kinds:

| provenance | meaning | changing it means |
|---|---|---|
| `derived` | follows from a definition or identity | the definition changed |
| `measured` | produced by an analysis in this repo, named in the entry | re-running that analysis |
| `external` | a fact about the world or a venue, dated | re-checking, because these expire |
| `declared` | somebody chose it | nothing here supports it |

**15 of 23 are `declared`.** That is the honest state of a project with no record yet, and stating it
as a proportion is more useful than defending each one individually. A `declared` entry must name
what would replace it — the registry refuses to construct one otherwise, because a declared
parameter with no replacement path is indistinguishable from a measurement nobody made.

The list is meant to shrink. `min_edge_bp` is waiting on realised costs from
`shadow.adverse_selection_report()`; the influence caps on out-of-sample Brier; the assumed source
correlations on observed error agreement. Each of those is blocked on the same thing as everything
else here — a record.

---

## 14. Unverified facts

Confirmed 19 Sep 2026 and time-sensitive; re-check before relying on any of it.

| Item | Status |
|---|---|
| UK close-only on frontend **and** API | Confirmed via venue geoblock docs |
| Polymarket CLOB V2 live 28 Apr 2026; **V1 clients unsupported in production** | Confirmed |
| `py_clob_client` (V1) — **do not use** | v1 of this design recorded it "name verified" from PolyBench's requirements, which is a *legacy* dependency. Verifying a name is not verifying an integration. Use `Polymarket/py-sdk`. |
| Feast default PIT join can return later backfills | Confirmed via Feast docs |
| Fees differ by market category | Reported; verify per market |
| Image digests, ALFRED/IMF/EIA vintage coverage, UMA dispute mechanics | **UNVERIFIED** |

---

## 15. What could end this

1. ~~**Eligibility.**~~ **Resolved** (§2.1): UK, close-only, paper-only posture. This no longer
   ends the project — it bounds what success means. P3 is answered as a research question.
2. **Serial dependence — now quantified.** Measured in Phase 1: `r_between` above 0.0032 makes the
   gate unreachable at any duration, and ≥12 independent regimes are required for the test to be
   valid at all. The real value of `r_between` is still unknown and only resolved forecasts can
   supply it (§3.2).
3. **Execution.** Shadow fills may show the edge does not survive the spread — which is why it now
   runs from Phase 2 rather than Phase 5.
4. **Priced-in evidence.** The market-conditioned model may show the evidence system adds nothing
   beyond the price. This is the most likely *scientific* failure and the ablations are designed to
   surface it early.
5. **Cluster saturation.** If the tradeable universe is dominated by correlated questions, `n_eff`
   accumulates too slowly regardless of throughput.
6. **Stage 3 cost.** If incentive analysis costs a day per forecast, the timeline doubles or worse.

Nothing in this document establishes an accuracy or profitability advantage. The next honest
milestone is a narrow prospective experiment with frozen rules, explicit abstention, and shadow
execution running alongside it from the first day.

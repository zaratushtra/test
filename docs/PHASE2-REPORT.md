# Phase 2 Report — Scoring and Decision Layers

**Date:** 19 Sep 2026
**Status:** **Partial.** The two layers buildable without market access are complete and validated.
Contract-registry population and the ablation harness remain blocked on Phase 0.
**Validation:** `tests/test_phase2.py` — 67/67. `tests/test_phase2b.py` — 43/43. Suites from earlier phases still green (36/36, 33/33).

---

## 1. What was built

### `spine/scoring.py`

Brier, Murphy decomposition, calibration curves, the regime-level bootstrap, and an estimator for
`r_between`.

The Phase 2 gate added after Phase 1 was that **the regime-level bootstrap must be the only scoring
path**. That is enforced rather than documented:

- No week-level or observation-level bootstrap is exported. A test asserts the module surface
  contains nothing matching `week`.
- `regime_bootstrap_ci()` **raises** below 12 regimes instead of returning a number. The message
  quotes the measured false-positive rates (43.5% at one regime, 13.0% at four) and says what to do
  about it.

The reasoning is that an invalid analysis documented as invalid eventually gets run. Making it
unavailable costs nothing and removes the failure mode.

`estimate_r_between()` closes the loop from Phase 1: the quantity that imposes a hard ceiling of
`1/r` on effective sample size is now computed from the record via a one-way random-effects ICC,
rather than assumed. Validated in both directions — near zero for independent regimes, and detecting
an injected per-regime shift (0.0000 → 0.0412, ceiling ∞ → 24).

### `spine/decision.py`

Book walking, conservative EV, abstention.

Three behaviours the module refuses, each of which flatters a backtest:

| Refused | Enforced instead |
|---|---|
| Pricing at the midpoint | `walk_book()` consumes levels cheapest-first to the intended size |
| EV from the point estimate | EV uses the end of the interval that argues *against* the trade |
| Assuming a fill | Depth shortfall is reported; `require_full_fill` defaults on |

The `§10.2` worked example is now an executable test: 64% central, 60% conservative, 61¢ executable,
1¢ costs → **−200bp, abstain** — and the decision records that the point estimate alone would have
given +200bp, so the uncertainty is visibly what killed the trade.

Eligibility is checked first: `close_only` abstains even when the edge is enormous. No amount of
edge makes an impermissible trade permissible.

---

## 2. One finding: the Murphy identity does not hold for continuous forecasts

The first test run failed on `B = reliability − resolution + uncertainty`. Investigation rather than
tolerance-fiddling showed the classical identity is exact only for **discrete** forecast values,
where every forecast in a bin is identical. With continuous forecasts the slack is the within-bin
variance:

| bins | residual |
|---|---|
| 5 | 0.002932 |
| 20 | 0.000218 |
| 101 | 0.000000 |
| discrete 5-value forecasts, 10 bins | 2.8 × 10⁻¹⁷ |

The code was right; the assertion was wrong. Fixed by renaming the field to `within_bin_residual`,
documenting why it exists, and replacing the test with two checks that are actually true: exactness
for discrete inputs, and monotone shrinkage as bins approach the number of distinct values.

A related trap now documented in the docstring: **binned reliability is biased upward as bins get
finer**, because fewer observations per bin make the observed rate noisier. Reliability may be
compared across models at a fixed bin count, never across bin counts. Reporting a reliability
improvement obtained by coarsening the bins would be a straightforward way to fake calibration.

---

## 2b. Second finding: peeking costs an order of magnitude

§12 asserted that repeatedly checking a 95% interval is p-hacking, and left it there. Assertion is
cheap, so it was measured (`phase1/sequential_peeking.py`) the same way the bootstrap failure was.

Fixed 95% lower bound, **true effect zero**, checked at every look and stopping at the first pass:

| looks | final look only | checked every look |
|---|---|---|
| 1 | 4.2% | 4.2% |
| 5 | 2.7% | 10.1% |
| 10 | 2.2% | 12.1% |
| 20 | 3.0% | 14.9% |
| 52 | 3.4% | **19.3%** |

A weekly check over one year turns a ~3% gate into 19.3%, a six-fold inflation.

(The single-look column sits near 3% rather than exactly 2.5%, with the one-look case at 4.2%. That
is the normal approximation being anti-conservative at small n — twelve observations with a plug-in
standard deviation — plus Monte Carlo noise at 1500 trials. It does not affect the comparison, which
is between columns at the same n.) **No bad faith is required** — the
gate is cheap, data accumulates, and someone looks. A rule that depends on people not looking will
fail, so `spine/sequential.py` budgets the looking instead.

Alpha spending restores the nominal rate at negligible cost in power:

| looks | spending | false positives | power @ effect 0.5 |
|---|---|---|---|
| 20 | Pocock | 3.3% | 100% |
| 20 | O'Brien-Fleming | 1.7% | 100% |
| 52 | O'Brien-Fleming | **1.1%** | 100% |

O'Brien-Fleming is the default: severe early, approaching nominal at the end, so an early stop is
rare and meaningful and almost all power is preserved for the full schedule. At 20 looks its fourth
threshold is z = 4.24 against a final z = 2.53, versus a nominal one-sided 1.645.

**The number of looks must be declared before the record starts accumulating.** Choosing it
afterwards, once the shape of the data is visible, reintroduces exactly the freedom the schedule
removes.

An incidental fix: the first implementation recomputed the standard deviation over the whole record
at every look, which is O(n²) and made a 52-look schedule impractically slow. Replaced with running
sums.

---

## 2c. Models and ablations

### `spine/models.py`

**Deadlines are a hazard problem.** §5.3 objected that "will X happen by D" is not a static
probability with a date attached, and v1 treated it as one. Implemented properly: if events arrive
at rate λ over an exposure window, `P(by D) = 1 − exp(−λ·t)`, and the reference class supplies λ as
*events per unit exposure* rather than `k/n`.

The consequence is visible in the fixture — identical `k/n` over ten times the exposure gives less
than half the probability, and the estimate decays as the window closes:

| time remaining | probability |
|---|---|
| 0.1 | 99bp |
| 1.0 | 952bp |
| 10.0 | 6321bp |

A static estimator returns the same number at all three, which is what the design rejected.

**The independent forecast widens as evidence arrives.** Evidence moves the estimate *and* adds
model risk; a forecast that grew more confident purely because more news arrived would be counting
corroboration as certainty. Measured: base interval 1588bp → 3769bp after two contributions.

**The market-conditioned model shrinks toward the market, not toward zero.** With `a=0, b=1` and no
adjustment it returns the price exactly — which is the correct null for "we have no edge". A
conventional ridge toward zero would instead predict 50% on everything, which is a confident and
wrong prior rather than a humble one.

### `spine/ablation.py`

Comparisons are **paired by key** on shared questions at matched cutoffs. Unkeyed observations,
duplicate keys, disjoint question sets and heavily filtered intersections are all *refused* rather
than warned about — comparing two models on different question sets measures the questions.

The twelve-regime requirement from Phase 1 applies unchanged; there is no laxer path for ablations,
because paired differences inherit exactly the same serial-dependence problem.

`beats_market()` exists as a separate named entry point for the one comparison that decides whether
any of this has a point, so it cannot be buried in a table of seven others.

### Two fixture corrections worth recording

Both were my test assumptions being wrong rather than the code:

1. **`hazard == static` when exposure is one unit per case** — I asserted they must differ. They
   correctly coincide in that case; the distinction only bites when exposure differs from case
   count, which is now what the test checks.
2. **The synthetic generator was calibrated at `edge=0.5`, not 1.0.** So the variant labelled "full"
   at 0.8 was *overconfident*, and the harness correctly scored it slightly worse than the
   "market" variant at 0.5. That was the ablation machinery working — detecting overconfidence as a
   loss — while my labels implied the opposite. Generator fixed so `edge=1.0` reproduces the truth.

---

## 3. Gate status

| Phase 2 gate item | State |
|---|---|
| `regime_id` populated and regime-level bootstrap the only scoring path | **done** — enforced and tested |
| Forecasts registering and scoring | **done** — registration in Phase 1, scoring here |
| One or two event families, frozen selection rules | blocked — needs the contract registry |
| Baseline + independent + market-conditioned forecasts | **done** — `spine/models.py`, deadline-aware, validated |
| Ablation harness | **done** — `spine/ablation.py`, paired, regime-gated |
| Explicit abstention | **done** — every refusal carries a reason |
| Shadow execution in parallel | partial — book walking and fills exist; needs recorded books |
| Sequential testing procedure (§12) | **done** — `spine/sequential.py`, measured and validated |

---

## 4. What Phase 2 has not established

- No real forecast has been scored. Every number above comes from synthetic fixtures with a known
  generating process.
- `min_edge_bp` defaults to 100bp on judgement, not measurement. The right value depends on the
  noise in our own price estimates, which is unknown until real books are recorded.
- Queue position, cancellation latency and adverse selection on passive fills are **named in the
  design and not yet modelled**. `require_full_fill` is a blunt substitute.
- Nothing here bears on whether the method has predictive skill.

---

## 5. Next

Phase 2 cannot complete without Phase 0. The blocking items, in order of how cheaply they resolve:

1. **Declare the operating jurisdiction.** Ten minutes. Determines whether the decision layer is
   ever used in anger or remains a research instrument.
2. **Reach the Gamma API** from a host that is not this sandbox, and run `phase0/screen_markets.py`.
   That populates the contract registry and unblocks event-family selection.
3. **Record order books** alongside the first forecasts so shadow execution has something to walk.

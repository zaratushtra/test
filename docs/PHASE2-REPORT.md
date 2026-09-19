# Phase 2 Report — Scoring and Decision Layers

**Date:** 19 Sep 2026
**Status:** **Partial.** The two layers buildable without market access are complete and validated.
Contract-registry population and the ablation harness remain blocked on Phase 0.
**Validation:** `tests/test_phase2.py` — 47/47. Suites from earlier phases still green (36/36, 33/33).

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

## 3. Gate status

| Phase 2 gate item | State |
|---|---|
| `regime_id` populated and regime-level bootstrap the only scoring path | **done** — enforced and tested |
| Forecasts registering and scoring | **done** — registration in Phase 1, scoring here |
| One or two event families, frozen selection rules | blocked — needs the contract registry |
| Baseline + independent + market-conditioned forecasts | partial — kinds enforced at registration; the models themselves need market data |
| Explicit abstention | **done** — every refusal carries a reason |
| Shadow execution in parallel | partial — book walking and fills exist; needs recorded books |
| Ablation harness | not started — needs real forecasts to ablate |

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

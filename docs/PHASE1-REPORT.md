# Phase 1 Report — Ledger, Registry, and the Serial Dependence Result

**Date:** 19 Sep 2026
**Status:** Phase 1 gates **PASSED**, with one finding that changes the Phase 3 gate.
**Validation:** `tests/test_phase1.py` — 33/33. `db/test_schema_v2.py` — 36/36.

---

## 1. Gates

SPINE-DESIGN-V2 §13 set three gates for Phase 1. All three pass.

| Gate | Evidence |
|---|---|
| The correction regression test passes | `test_phase1.py [4]` — a value available at 10:00 and corrected at 11:00; a forecast reconstructed for 10:30 receives **only** the original |
| A forecast reconstructs from its manifest alone | `test_phase1.py [7]` — manifest content, frozen reference class, and effects available at decision time all recovered; recomputing the hash from stored fields reproduces it |
| The chain is anchored externally | `test_phase1.py [3]` — anchor coverage is reported, and records past the last anchor are explicitly flagged as tamper-evident but **not time-proven** |

### What was built

| Module | Role |
|---|---|
| `spine/canonical.py` | RFC 8785-style canonicalisation, content hashing, chain hashing. NaN/Infinity rejected rather than tolerated. Golden vector pinned in the test suite. |
| `spine/chain.py` | Append, verify by recomputation, external anchoring. `verify()` reports anchor coverage rather than implying the chain dates itself. |
| `spine/ledger.py` | Availability-time retrieval, content-addressed manifests, forecast registration, reconstruction. |
| `db/schema_v2.sql` | 20 STRICT tables (unchanged this phase). |

Two design decisions worth recording:

**Guards live in the schema, not in Python.** `register_forecast()` is deliberately thin — interval coherence, minimum width, chain continuity and reference-class ordering are all database constraints. A guard implemented twice eventually disagrees with itself.

**Reproducibility means auditable, not re-runnable.** For LLM-assisted forecasts the manifest holds prompts, retrieved context and raw output, because inference endpoints drift and retire. `reconstruct()` returns what a reviewer needs to verify what was done, which is achievable, rather than promising bit-identical replay, which is not.

An incidental result from the tamper test: the first attempt tried to corrupt a stored probability and was **rejected by a CHECK constraint** before the hash could even be consulted. Tampering had to be redirected to an unconstrained field to test the hash at all. Two independent layers, both working.

---

## 2. The serial dependence finding

§3.2 flagged this as the largest open risk. It is worse than flagged, and in a different way.

### 2.1 Effective sample size has a ceiling that time cannot lift

Model a component `g` shared across **all** observations — one analyst, one frozen model version, one macro regime — alongside the within-week component:

```
d_ij = mu + g + u_i + e_ij
r_between = gamma² / V
```

Under equicorrelation, `n_eff = n/(1+(n−1)r) → 1/r`. So:

| `r_between` | n_eff ceiling | n_eff @1yr | n_eff @5yr | weeks to n_eff = 314 |
|---|---|---|---|---|
| 0.0000 | unbounded | 308 | 1541 | 53 |
| 0.0010 | 1000 | 237 | 608 | 77 |
| 0.0032 | 312 | 157 | 261 | **unreachable** |
| 0.0050 | 200 | 123 | 178 | **unreachable** |
| 0.0500 | 20 | 19 | 20 | **unreachable** |

**Between-week correlation must stay below 0.0032 for the Phase 3 gate to be reachable at all.** Above that, the ceiling sits below the requirement and no amount of running time closes the gap.

### 2.2 The bigger problem: the gate's false-positive rate is inflated

Simulating the decision rule as specified — cluster bootstrap over weeks, 2.5th percentile — with a *true effect of zero*:

| `r_between` | weeks | gate fires on noise |
|---|---|---|
| 0.000 | 52 | 2.5% (nominal) |
| 0.005 | 52 | **15.0%** |
| 0.050 | 52 | **37.0%** |
| 0.050 | 260 | **38.0%** |

Running five times longer does not help. The gate as designed would have declared success on pure noise roughly a third of the time.

### 2.3 Mechanism

The grand mean is **not consistent** when a global component exists. Its variance tends to `gamma²`, not zero — while a week-level bootstrap, which cannot see `g` because every resample contains it, keeps shrinking the interval:

| `r_between` | weeks | sd(grand mean) | mean CI half-width | |
|---|---|---|---|---|
| 0.00 | 52 | 0.0530 | 0.1093 | ok |
| 0.00 | 1040 | 0.0111 | 0.0250 | ok |
| 0.05 | 52 | 0.2387 | 0.0911 | too narrow |
| 0.05 | 260 | 0.2045 | 0.0416 | too narrow |
| 0.05 | 1040 | 0.2011 | 0.0210 | **10× too narrow** |

At 1040 weeks the true sampling variability has plateaued at 0.20 (theory: `gamma` = 0.2236) while the reported interval is a tenth of that. **This is not a slow gate. It is an invalid one.**

### 2.4 The fix, and what it costs

You cannot bootstrap away a component shared by every observation. It has to be *sampled* — which means rotating whatever it is constant within (model version, analyst, macro period) and bootstrapping at **that** level:

| independent regimes | weeks each | false-positive rate | power |
|---|---|---|---|
| 1 | 52 | 43.5% | 82% |
| 4 | 13 | 13.0% | 66% |
| **12** | **13** | **4.0%** (nominal) | **94%** |
| 24 | 13 | 3.0% (nominal) | 100% |

**At least ~12 independent regimes are needed to restore nominal error rates.** One regime is worthless at any duration.

This relocates the hard problem. It is no longer "run longer" — it is deliberately varying the shared component, which conflicts with the instinct to freeze everything for reproducibility. Both are needed: freeze *within* a regime so each is reconstructible, rotate *across* regimes so `g` is sampled rather than baked in.

---

## 3. Changes this forces

1. **The Phase 3 gate is rewritten.** Bootstrap over regimes, not weeks. Minimum 12 independent regimes. `n_eff` alone is not a sufficient stopping rule.
2. **`r_between` becomes a measured deliverable.** Phase 3 must estimate it from the accumulating record and re-derive its own stopping rule, rather than assuming a value.
3. **Regime rotation is a design requirement**, recorded per forecast so the bootstrap can group by it.
4. **Every timeline in §13 remains provisional** until `r_between` is measured on real forecasts. The simulation bounds the problem; it does not measure the actual value.

---

## 4. What Phase 1 did not establish

- No forecast has been made. The ledger is exercised only by synthetic fixtures.
- `r_between` is simulated across a plausible range, **not measured**. The real value is unknown and can only come from resolved forecasts.
- Phase 0's market screen and vintage audit remain blocked: Polymarket and FRED are unreachable from the build environment, and jurisdiction is still undeclared.
- Nothing here bears on whether the method has predictive skill. Phase 1 is plumbing and integrity only.

---

## 5. Next — Phase 2

Contract registry population, one or two event families, baseline + independent + market-conditioned forecasts, frozen selection rules, explicit abstention, **with shadow execution running in parallel from the start**.

The Phase 2 gate should add one item in light of §2: **the regime field must be populated and the regime-level bootstrap must be the only scoring path**, so the invalid week-level rule cannot be used by accident.

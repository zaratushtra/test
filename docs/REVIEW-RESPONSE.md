# Response to SPINE Design Review (19 Sep 2026)

> **Section references in this document are to SPINE v1**, the design under review. v2 renumbered
> extensively; `tests/test_docs.py` treats a document carrying this notice as historical.
I independently reproduced the database probes, re-derived the contested statistics, and checked
the three external claims. **The review is right on essentially everything material.** Four of its
findings invalidate things I stated with more confidence than I had earned, and one of them — the
probability firewall — I made *worse* in a previous round while believing I was fixing it.

Below: what I verified, what I accept, where I'd refine rather than adopt the proposed fix, and
what this does to the plan.

---

## Verification log

**Database probes — all six reproduce**, against the schema exactly as published, SQLite 3.45.1:

| Probe | My result | Matches review |
|---|---|---|
| `probability_bp = 5000.5` | ACCEPTED, `typeof = real` | yes |
| `base_probability_bp = 15000` | ACCEPTED | yes |
| UPDATE a frozen reference class | ACCEPTED (n 20→999, k 1→500) | yes |
| UPDATE an existing signal's `llr` | ACCEPTED (0.5 → 99.0) | yes |
| Insert `A→B` then `B→A` | ACCEPTED | yes |
| Insert `signal_items` | FAILED — `no such table: main.sources` | yes |
| *Positive control:* UPDATE a forecast | correctly REJECTED | yes |

Two I found that the review didn't list:

- `probability_bp = '5000'` **as a text string is also accepted.**
- **A Layer 3 forecast at p = 0.02 is rejected by the CHECK constraint.** That is §1 of the review
  demonstrated live: the database refuses to record a legitimate 2% ten-day forecast.

**External claims — all three confirmed:**

- **Polymarket V1 clients are dead.** CLOB V2 went live 28 April 2026; legacy V1 SDKs and V1-signed
  orders are unsupported on production. The legacy `py-clob-client` repo redirects to
  `Polymarket/py-sdk`.
- **UK is close-only on both frontend and API** — no new positions, no Gambling Commission licence.
  (Ireland and the Netherlands are frontend-only; the UK is both.)
- **Feast does not do this by default.** Its docs state that by default Feast does not compare the
  created timestamp against the entity row's event timestamp, so a correction or backfill created
  *after* the lookup time can still be returned. `created_timestamp_column` must be configured
  explicitly, and behaviour varies by offline store.

I also confirmed both proposed schema fixes work: a `BEFORE INSERT` trigger with a recursive CTE
catches 2-cycles, 3-cycles and self-edges while correctly permitting diamonds; `STRICT` tables
reject REAL in an INTEGER column. (STRICT still accepts the text `'5000'`, coercing it losslessly to
integer 5000 — semantically fine, but worth knowing.)

---

## Accepted without qualification

Ranked by how much damage each would have done.

**1. The probability firewall is wrong, and my "fix" made it worse.**
The bands confuse *the probability of an event* with *confidence in the estimate*. A ten-day event
can legitimately be 3% or 97%. Worse: in an earlier round I identified that the original cap only
constrained one tail and "fixed" it by making it symmetric — `abs(p − 0.5) ≤ cap − 0.5`. The
original at least permitted a 2% forecast. My correction banned it in both directions, and the probe
above shows the database now refusing exactly the forecast you would most want to make. I made a
wrong control rigorously wrong and called it a blocker fix.

The replacement is right: store estimated probability, estimation uncertainty, and trading
permission/exposure as three separate things. Also correct on the mechanism — a `CHECK` constraint
*rejects a row*; it does not clamp a value. The design's repeated claim that the firewall "clamps
the result" was never true of the SQL.

**2. Weekly `n_eff` cannot be summed across weeks.**
I assumed linear accumulation without examining it. The review's arithmetic is right: 8/week at
within-week r = 0.05 gives 5.93, and 42 independent weeks gives ~249 — but if r = 0.05 applies
across all 336 observations, `n_eff` is 336/(1 + 335×0.05) ≈ **18.9**. Same-analyst, same-model-
version, same-macro-regime dependence across weeks is entirely plausible. This could be an
order-of-magnitude error in the headline timeline, and nothing in the design or the fixture tests it.

**3. The evidence→probability mapping does not exist.**
`LLR_c` is stored per *cluster*, unbound to contract or horizon. The committee-vote example is
decisive: one fact, three contracts, three different likelihood ratios. The correct object is
`LLR_{c,m,t}` conditioned on information already incorporated. Separating claims from
claim-to-contract effects, and preferring a trainable event-family model over hand-assigned ratios,
is the right architecture. An LLM proposing "likelihood ratio 3.2" with a persuasive explanation is
not a measurement.

**4. The power calculation has a real coefficient error.**
I derived with a one-sided α = 0.05 (z = 1.645) but specified a gate using the 2.5th bootstrap
percentile, which is one-sided 2.5% (z = 1.96). Consistent with the stated gate the coefficient is
**(1.96 + 0.842)² × 4 = 31.4**, not 24.7. Every number in my §2.2 table is ~27% too low.

**5. "Any pass would be noise" was a logic error.**
Low power means a low probability of *detecting* an effect of the specified size. It does not mean a
result that does fire is noise. The practical conclusion — `n_eff = 10` is not a usable gate —
survives and in fact strengthens under the corrected coefficient, but the reasoning I used to reach
it was wrong and stated with unearned force.

**6. The power derivation substitutes the wrong variance.** `Var(d) = 4E[δ²p_m(1−p_m)] + Var(δ²)`.
I used benchmark variance `p_b(1−p_b)` in place of the model-conditional term and dropped the
second entirely. The "base-rate independence" I highlighted is an artifact of that substitution, not
a property of the problem. Simulation over the actual question mix and decision rule is the right
method; the table is an illustrative scenario, not a bound.

**7. Stage 4 omits a term.** `P(B) = P(B|A)P(A)` holds only when B cannot occur without A. Law of
total probability requires `+ P(B|¬A)P(¬A)`. Plain error.

**8. Six schema guarantees are prose, not enforcement** — all reproduced above. The `sources` table
is simply undefined; `signals` is described as append-only with no trigger; `reference_classes` is
mutable after freezing; `base_probability_bp` is unbounded; the type is unenforced; cycles pass.

**9. A hash chain does not establish when a forecast existed.** It proves internal consistency and
detects tampering *within* a chain. It does not prevent fabricating an entire chain later with
backdated timestamps. Pre-registration is the system's core credibility claim and it is currently
unsecured. It needs external anchoring — RFC 3161 timestamping or publishing the chain head to an
independent append-only log.

**10. Commit to inputs, not their names.** `lambda_version` is a label; changing what it points to
changes reconstructibility without changing the forecast hash. Needs a content-addressed manifest
covering data snapshot, evidence, model config, source-reliability version and market rules — and
for LLM-assisted forecasts, the actual prompts, retrieved context and raw output.

**11. `frozen_at < created_at` proves almost nothing.** It is necessary and wildly insufficient for
"the analyst had not seen the case." That needs procedural control, not a timestamp comparison.

**12. Availability time ≠ arrival time.** Raw item at 10:00, extraction 10:02, verification 10:05 —
a decision at 10:01 must not see the verified signal. Keying everything on `first_seen_at` is wrong.
The five-way split (event / claimed publication / collector observation / artifact creation /
available-for-decision) is correct, and the 10:00-value-corrected-at-11:00 regression test should be
mandatory.

**13. The Feast recommendation was overstated** — confirmed above. It is a useful tool that requires
explicit configuration, not a safeguard you inherit.

**14. Training splits must respect label availability.** Overlapping multi-year forecasts mean an
example created pre-cutoff can resolve post-cutoff. Needs `label_available_at` and purging.
Standard purged/embargoed CV discipline that I omitted.

**15. More than two free parameters.** I named λ and λ_sig. Reference-class selection, feature
weights, ρ, clustering thresholds, source weights and market-selection rules are all researcher
degrees of freedom and all need governance.

**16. Source binding is not truth verification.** The four-way split (authenticity / faithful
extraction / truth / contract relevance) is better than what I had. "Bound to a primary source needs
no corroboration" is too strong — an authentic statement that negotiations are progressing
establishes only that the statement was issued.

**17. Co-publication measures coverage overlap, not error independence.** Two outlets with their own
correspondents on the same scheduled event co-occur constantly with independent errors; two
unrelated outlets sharing one anonymous source have correlated errors and no co-publication signal.
The provenance graph (artifact → claim → cited upstream evidence → reporting origin → contract) is
the right structure. My publication-lag heuristic would also wrongly discount genuine late
confirmation.

**18. The `L_max` ceiling does not hold.** The contribution is `λ_sig · w_c · LLR_c`, and `w_c > 1`
whenever `n_eff_sources > N_ref`. The stated 4.5:1 cap was false as written. Any influence budget
must bind the final contribution.

**19. Contradiction handling is an attack surface.** Automatically downweighting on contradiction
lets an unsupported denial dilute strong evidence. Adjudicate asymmetrically.

**20. Positive BSS does not prove calibration.** Skill can improve through resolution while
reliability stays poor. My claim that passing Register A "demonstrates that the T1 model is
calibrated" is wrong — it demonstrates skill. Calibration is the reliability term specifically.

**21. Contract ≠ world event, and for trading the binding must be enforced.** Separating the research
proposition from the tradeable contract is right. The UMA "Unknown / 50-50" payout is the sharpest
case: a position can pay $0.50 while the binary research outcome is `void` and excluded from Brier.
Under my design that position would vanish from evaluation while having actually lost money.

**22. Midpoint is not an executable price**, and the EV formula with expected acquisition price at
intended size is correct. The worked example (64% estimate → 60% conservative → 61¢ price → 1¢
costs → −2¢, abstain) is the right operational output shape.

**23. Paper trading must start earlier.** My roadmap put shadow execution after a 10–18 month
accuracy gate. If execution eats the edge you learn that at month eighteen. Running order-book
collection and shadow fills *alongside* prospective forecasting is strictly better sequencing, and
it costs nothing to do.

**24. The halt state is unspecified where it matters.** Stopping the strategy does not cancel
resting orders, and switching to paper does not remove existing exposure. The correct semantics —
no new exposure, cancellation and reconciliation *stay live*, monitoring continues, positions remain
under active risk management — is right. So is an expiring health lease over a persistent flag: a
dead monitor cannot clear a flag, but a lease fails safe on its own.

**25. Prompt injection.** Retrieved news reaching a system with trading authority is an attack path I
never addressed. Signer and risk checks must sit outside LLM authority; retrieved documents are
untrusted input throughout.

**26. The scope conflict is real.** Design §7 Phase 0 says trading is *deleted* unless enough
long-dated structural markets exist. Signals §11.0 says T1/T2 trade and T3 is forecast-only. I
introduced the tiering without reconciling it with the gate it contradicts. Both cannot govern.

**27. `py_clob_client` "name verified" was verifying the wrong thing.** I confirmed a name from
PolyBench's `requirements.txt` — a *legacy* V1 dependency — and recorded it as a positive
verification. V1 is unsupported in production. This is a clean demonstration of the review's point
that name verification is not integration verification, and it happened in the same document where I
was lecturing about unverified claims.

---

## Accepted with refinement

Four places where the finding is right but I would land somewhere slightly different.

**A. Relocate the firewall, don't delete it.**
The band must go — that is settled. But the concern underneath it was real: credibility earned on
short-horizon claims leaking into long-horizon ones. Under the proposed three-way split that
discipline should survive as (i) a *minimum* uncertainty width by horizon class, and (ii) exposure
caps by horizon class. "Remove the firewall" risks discarding the motivation along with the broken
mechanism. Cap exposure and mandate uncertainty; never cap the probability of reality.

**B. The non-monotonicity finding is estimator-relative — which is an argument for changing the
estimator, not for dropping the finding.**
The review is correct that additional correlated observations need not reduce information available
to a *sensibly weighted* analysis; under GLS-style weighting they weakly increase Fisher information.
But the Register A gate as specified uses an **equally-weighted** cluster bootstrap, and under that
decision rule the non-monotonicity is real. So the right conclusion is: replace the estimator with
one that downweights rather than discards, and then the "one market per cluster" rule reduces to a
workload heuristic — which is how I should have framed it. I stated it as a statistical optimum on
the strength of a 20-market fixture, which it is not.

**C. `Beta(1, 3)` — right criticism, slightly overstated impact.**
Prior mean is indeed 0.25 and I wrongly described it as a rare-event default. In the estimator
`p₀ = (k+α)/(n+α+β)` it acts as four pseudo-observations, so at n = 20, k = 1 it moves 0.05 → 0.083:
material but not ruinous. The fix is per-family α, β fitted from the family's own historical base
rate rather than any universal default. The deeper point — that "will X happen by D" needs
denominators, exposure windows, censoring and elapsed time, i.e. a hazard model — is correct and
more consequential than the prior choice.

**D. Cluster-level exposure limits survive; sizing on `n_eff` does not.**
The conflation is real: `n_eff` measures evidence, not financial risk. But concentration control
across correlated positions is still necessary — eight markets downstream of one thesis is one bet
economically as well as evidentially. The correct version is a cluster exposure cap driven by *loss*
covariance, which is the review's own §4.4 point applied consistently. So: drop `n_eff` as the sizing
input, keep the cluster as the unit of concentration.

---

## What this does to the plan

**Jurisdiction is now Phase 0 item zero.** If operations are UK-based, the UK is close-only on both
frontend and API — T1/T2 trading cannot open new positions at all, and the entire betting premise is
moot regardless of everything else in this document. **This needs answering before any further
design work**, because it determines whether we are building a trading system with a research arm or
a research system, full stop. It is a ten-minute check that could save months.

**The design needs a v2, not patches.** Findings 1–3 are load-bearing. Patching the probability
model, the evidence mapping and the validation gates section-by-section would leave a document whose
parts no longer agree. I would rather rewrite `SPINE-DESIGN-FINAL` and fold `SPINE-SIGNALS` into it
as a single document, restructured around the review's three-proposition frame: *a verified fact is
not predictive information; predictive information is not an opportunity at the available price.*
Those want testing separately, and the current architecture cannot.

**Concrete work already validated and ready to apply:**

- Schema v2 — `STRICT` tables, bounds on every probability column, versioned immutable reference
  classes, append-only `signals`, recursive-CTE cycle detection, a defined `sources` table,
  `available_for_decision_at`, settlement/payout separate from binary outcome, content-addressed
  manifest reference, external anchor field. Both novel mechanisms tested above.
- `phase0/screen_markets.py` — add a jurisdiction/eligibility gate and switch the power coefficient
  from 24.7 to 31.4, which moves every projection out by ~27%.
- `SPINE-SIGNALS` §11.10 — the `py_clob_client` row is wrong and needs replacing with
  `Polymarket/py-sdk`, plus a note that the previous "verified" annotation verified the wrong
  property. The Feast row needs the `created_timestamp_column` caveat.

**What I would not do yet:** build anything. The review's closing point stands — neither the
documents nor the fixture establish an accuracy or profitability advantage. The next honest
milestone is a narrow prospective experiment on one or two event families with frozen selection
rules, explicit abstention, and shadow execution running in parallel from day one.

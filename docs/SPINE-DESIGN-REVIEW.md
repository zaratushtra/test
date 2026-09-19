# SPINE Design Review and Improvement Recommendations

**Review date:** 19 September 2026  
**Designs reviewed:** `SPINE-DESIGN-FINAL.md` and `SPINE-SIGNALS.md`

*Full review exported from the conversation. Chat-specific source citations have been converted to Markdown footnotes; equations are preserved as LaTeX.*

---

## Overall assessment

**Keep SPINE’s provenance and audit foundations, but redesign its probability model and validation gates before implementing it as a betting system.**

The designs contain useful controls: immutable forecast records, attention to data availability, syndication detection, separate evaluation across horizons, and a trading subsystem that is not enabled by default. However, several mathematical and architectural assumptions would undermine the accuracy you are trying to achieve. The most consequential are the probability caps, the undefined mapping from verified claims to likelihood ratios, and the use of effective sample size to justify the roadmap.

**The central distinction is this: a verified fact is not necessarily predictive information, and predictive information is not necessarily an opportunity at the available price.** SPINE needs to test those three propositions separately.

I reviewed both documents, checked selected current platform and dependency documentation, and executed the supplied SQL in an in-memory SQLite database. The database findings below are reproduced results; the forecasting recommendations are design proposals, not demonstrated trading performance.

One immediate deployment constraint: Polymarket’s current API documentation lists the **United Kingdom as close-only on both frontend and API**, meaning no new positions. Its help centre uses broader restricted-country wording. Both rule out assuming that new UK-based trading is permitted; eligibility should be a Phase 0 gate, not something deferred until deployment. ([docs.polymarket.com](https://docs.polymarket.com/api-reference/geoblock))

## 1. Remove the probability firewall

### The existing control prevents accurate forecasts

The SQL permits Layer 3 probabilities only between **40% and 60%**, Layer 2 between 30% and 70%, and Layer 1 between 15% and 85%. The signal addendum explicitly retains this restriction for T1. [^design-263-267] [^signals-350-356]

This confuses **event probability** with **confidence in the estimate**.

A short-horizon event can legitimately have a 3%, 50%, or 97% probability. Its being ten days away does not make 40–60% an appropriate range. The restriction either rejects useful forecasts or distorts them if the application implements the stated clamping.

For illustration, suppose an event’s true probability is 5%. Its expected Brier score is:

| Published probability | Expected Brier score |
|---|---:|
| 5% | 0.0475 |
| 40%, after the proposed cap | 0.1700 |

The cap introduces **0.1225 of avoidable expected error**. This follows directly from the Brier formula; it is not an empirical claim about your model.

There is also a smaller implementation error: a SQL `CHECK` constraint **rejects** an invalid row; it does not clamp its value.

### Recommended replacement

Store separately:

- **Estimated probability:** the model’s best estimate of the outcome.
- **Estimation uncertainty:** how uncertain you are about that probability, based on a specified statistical method and its assumptions.
- **Trading permission and exposure:** whether the estimate is reliable enough to act on, and the permitted financial risk.

When evidence is weak, shrink forecasts toward an appropriate benchmark or abstain. **Cap exposure, not the probability of reality.**

For example, “estimated probability 8%, but insufficient evidence to trade” is a coherent output. “We cannot say less than 40% because this is T1” is not.

## 2. The missing core is the mapping from evidence to probabilities

The proposed signal update is:

$$
\operatorname{logit}(p_t)
=
\operatorname{logit}(p_0)
+
\lambda_{\rm sig}\sum_c w_c\,LLR_c.
$$

But the design does not specify how a statistically meaningful `LLR_c` is obtained. The `signals` table stores an `llr` without binding it to a market, target outcome, or forecast horizon. [^signals-337-360] [^signals-410-421]

**That is the most important missing modelling component.**

Consider a hypothetical verified claim:

> A bill passed a parliamentary committee.

The same fact could strongly affect “Will it reach a floor vote this month?”, weakly affect “Will it become law this year?”, and have almost no bearing on “Will it be enacted by tomorrow?”

There cannot be one universally applicable likelihood ratio attached to that story.

A meaningful likelihood ratio is target-specific:

$$
LLR_{c,m,t}
=
\log
\frac{P(E_c\mid Y_m=1,\mathcal I_{t^-})}
     {P(E_c\mid Y_m=0,\mathcal I_{t^-})},
$$

where $m$ identifies the exact contract and $\mathcal I_{t^-}$ is the information already incorporated.

The conditioning matters: a fact may be highly predictive in isolation but add almost nothing after related evidence is already known.

### What I would change

Separate **claims** from **claim-to-contract effects**.

A claim record should describe what was asserted, its source, time, evidential support, and verification status. A separate relationship should describe which contract it affects, through which mechanism, over what horizon, using which model and parameter version.

For the initial system, I would prefer a modest, trainable event-family model over hand-assigned likelihood ratios. For example, a regularised logistic model could combine procedural state, time remaining, relevant observations, and market information. Its coefficients would be estimated and evaluated out of sample.

An LLM can extract structured observations and propose causal relevance. An LLM-generated “likelihood ratio of 3.2” should not become a numeric input merely because its explanation sounds convincing.

### The market must be more than a late-stage comparator

I would maintain two forecasts:

**An independent forecast**, built without seeing the target market price, to assess what your evidence system knows independently.

**A market-conditioned forecast**, which estimates whether your evidence improves on the contemporaneous market benchmark.

One candidate model is:

$$
\operatorname{logit}(p_{m,t})
=
a_g+b_g\operatorname{logit}(q_{m,t})+f_g(x_t,h),
$$

where $g$ identifies an event family, $h$ is the remaining horizon, and $f_g$ is strongly regularised.

This is a proposed model class, not a guaranteed source of edge. Its purpose is to test **incremental information beyond the price**, rather than repeatedly adding publicly known facts to a prior that may already reflect them.

## 3. Strengthen verification: source binding is not truth verification

The addendum says that a claim bound to a primary source needs no corroboration. It also treats early publication as evidence of genuine reporting independence. Both rules are too strong. [^signals-292-303] [^signals-226-238]

### Distinguish four different questions

**Authenticity:** Is this really the official filing, statement, or dataset?

**Faithful extraction:** Does the extracted claim preserve the source’s negation, qualifications, scope, and date?

**Truth:** Does the artifact establish the underlying fact, or merely establish that someone asserted it?

**Contract relevance:** Does that fact affect the contract’s actual resolution condition?

An authentic statement that “negotiations are progressing” verifies that the statement was issued. It does not independently establish that an agreement is imminent.

Conversely, when a contract explicitly resolves on whether a particular authority publishes a particular announcement, the authentic announcement may directly establish the relevant condition. The treatment should depend on the claim and contract, not a blanket source label.

### Replace source-count confidence with evidence lineage

The proposed co-publication matrix measures which outlets cover similar events. That is not the same as measuring whether their **reporting errors are independent**. [^signals-228-234]

Two independent correspondents may repeatedly cover the same scheduled announcements. Two apparently unrelated outlets may both depend on the same anonymous source. A late report may be an independent confirmation rather than a copy.

I would construct a provenance graph connecting:

**Artifact → extracted claim → cited upstream evidence → reporting origin → contract.**

Use textual overlap, explicit attribution, timestamps, and reporting descriptions to identify shared evidence. Ownership and co-publication can be useful clues, but should not determine independence by themselves.

Also, avoid automatically reducing the weight of an established fact whenever a contradictory claim appears. Otherwise, an unsupported denial can dilute strong evidence. Preserve both claims, distinguish changing circumstances from genuine contradictions, and adjudicate support asymmetrically.

### The influence ceiling is not actually guaranteed

The addendum claims that `L_max ≈ 1.5` limits one cluster to approximately a 4.5:1 likelihood ratio. However, the actual update is multiplied by both `λ_sig` and `w_c`. Since `w_c` exceeds one when `n_eff_sources > N_ref`, the stated ceiling does not hold generally. [^signals-342-355]

Any influence budget must apply to the **final contribution**, including scaling and related claims—not just the unweighted `llr`.

## 4. Rework the statistical validation gates

The documents are right to worry about correlated observations. The problem is that the numerical treatment is being used more confidently than its assumptions justify.

### 4.1 The power formula is an approximation, not a feasibility theorem

The design derives:

$$
n_{\rm eff}\gtrsim 24.7/BSS
$$

and uses it to conclude that some earlier gates cannot detect any real skill. [^design-64-104]

That conclusion does not follow.

Let $\delta=p_m-p_b$, and assume the stronger calibration condition $E[Y\mid p_m,p_b]=p_m$. Then:

$$
E[d]=E[\delta^2],
$$

but the variance is:

$$
\operatorname{Var}(d)
=
4E[\delta^2p_m(1-p_m)]
+
\operatorname{Var}(\delta^2).
$$

The document substitutes benchmark variance for model-conditional variance and omits the second term. That can provide a useful approximation in restricted, small-effect settings. It is not a universal sample-size law.

In particular, **low power does not mean every statistically significant result must be noise**. It means the design has a low probability of detecting the specified effect under the assumed model.

There is also a mismatch between the one-sided 5% calculation and the gate’s use of the 2.5th bootstrap percentile. Under the same approximation, the latter would use a coefficient of approximately **31.4**, rather than 24.7. [^design-79-82] [^design-494-501]

**Recommendation:** simulate power for plausible distributions of paired score differences, actual question mix, temporal dependence, and the exact decision rule. Treat the current table as an illustrative scenario, not a project-ending mathematical bound.

### 4.2 The “Phase 0” fixture does not establish market independence

The addendum’s basket-size results come from a **20-market, eight-cluster fixture**. That can test a screening script’s calculations, but it does not measure how many sufficiently independent opportunities Polymarket actually offers. [^signals-58-88]

Its broader conclusion—that evidence necessarily falls after taking one market per cluster—is also too strong.

Adding highly correlated observations can reduce effective sample size for a particular **equally weighted estimator**, especially if it overweights one cluster. But additional observations do not have to reduce the information available to a sensibly weighted analysis: the analysis can downweight or ignore them.

“One market per cluster” may be a useful workload heuristic. It is not a general statistical optimum.

### 4.3 Weekly effective sample sizes cannot automatically be added

The roadmap assumes weekly effective sample sizes accumulate into the target total. That requires appropriate independence or sufficiently weak dependence **between weeks**, not just diversity within each week.

A numerical illustration:

- Eight forecasts per week with within-week correlation 0.05 give approximately 5.93 effective observations.
- Across 42 independent weeks, that would give approximately 249.
- But if correlation 0.05 applied across **all 336 observations**, the same formula would give only approximately **18.9**.

Neither assumption is established by the attached fixture. The example shows why serial dependence can dominate the timeline.

### 4.4 Measure the dependence relevant to the question

Different tasks require different dependence models:

**Forecast evaluation:** dependence between paired score differences.

**Portfolio risk:** dependence between monetary losses.

**Evidence verification:** dependence between reporting errors and shared upstream sources.

A single DAG correlation field or generic `n_eff` should not substitute for all three.

Finally, a positive Brier Skill Score does **not**, by itself, prove calibration. A model may improve overall Brier performance while remaining systematically overconfident in a region of its forecasts. The addendum’s claim that passing Register A demonstrates calibration should be narrowed accordingly. [^signals-104-106]

## 5. Correct the other probability-model assumptions

### Stage 4 omits a term

The stated equation is:

$$
P(B)=P(B\mid A)P(A).
$$

Generally, it must be:

$$
P(B)=P(B\mid A)P(A)+P(B\mid\neg A)P(\neg A).
$$

The shortened version is valid only when $B$ cannot occur without $A$. An ordinary causal relationship is not enough. [^design-470-474]

### The coordination penalty is an unvalidated model

The rule $p\leftarrow p\rho^{N-1}$, with $\rho\approx0.7$, imposes a specific multiplicative penalty for each additional actor. It does not follow merely from having an incentive analysis or payoff ordering. [^design-456-462]

Use the analysis to construct explicit conditional scenarios or features. Estimate their predictive effects, or clearly label them as subjective assumptions and test sensitivity.

### Reference classes need denominators and exposure windows

`Beta(1,3)` has a prior mean of **25%**; it is not an automatically suitable prior for every “rare event” class. More importantly, the design needs to define what counts as an opportunity for an event not to happen—not just collect historical events and their outcomes. [^design-215-234] [^design-418-425]

For “will X happen by date D?” questions, specify the observation window, eligible population, censoring, and treatment of elapsed time. A deadline-aware or survival model may be more appropriate than a static event probability.

Three horizon tiers also do not eliminate the need for event-family models. A court ruling, an economic release, and an election do not become the same statistical problem because they all resolve in ten days.

## 6. The database does not enforce several promised guarantees

I loaded the SQL from both attachments into an in-memory SQLite database without modifying the supplied definitions.

| Probe | Observed result | Required change |
|---|---|---|
| Insert `probability_bp = 5000.5` | Accepted and stored as `REAL` | Enforce integer type, not just column affinity |
| Insert `base_probability_bp = 15000` | Accepted | Add probability bounds |
| Change a reference class after freezing | Accepted | Immutable class versions and membership snapshots |
| Update an existing signal’s `llr` | Accepted | Enforce append-only signal history |
| Insert `A → B` and `B → A` | Accepted | Actual cycle detection |
| Insert a `signal_items` row | Failed: `no such table: main.sources` | Define the referenced `sources` table |

As a positive control, attempting to update an existing forecast **was correctly rejected**.

These are database-layer findings, not claims about an unseen application. Application code could add protections, but the attached design explicitly presents several of them as schema guarantees. The relevant definitions currently contain only a subset of those controls. [^design-241-280] [^design-323-354] [^signals-364-427]

### Hashing does not establish when a forecast existed

The document says that including `created_at` in the hash means backdating breaks verification. That detects changing a timestamp in an already committed record. It does **not** prevent someone from creating a new, internally consistent chain with fabricated old timestamps. [^design-374-381]

To prove pre-registration, periodically anchor the chain head in an independently timestamped, append-only destination. A private signature alone is not proof of publication time.

### Commit to the inputs, not merely their names

The forecast hash references identifiers and version labels, but does not commit to the full reference-class membership, parameter contents, selected signals, or raw evidence artifacts. Changing what a version label points to can change reconstructibility without changing the forecast hash. [^design-374-377]

Each forecast should reference a content-addressed manifest containing the exact data snapshot, evidence, model/configuration artifacts, source-reliability version, and applicable market rules. For LLM-assisted forecasts, retain the actual prompts, retrieved context, tool results, and raw output—not only their version labels.

### A freeze timestamp does not prove the case was unseen

The trigger checks that `frozen_at < created_at`. It cannot establish that an analyst selected the reference class before examining the case. [^design-348-354]

That requires a procedural control: predeclared family-level selection rules, logged decisions, and an auditable separation between defining a class and applying it.

## 7. Point-in-time correctness needs more than `first_seen_at` or Feast

Preserving first-seen timestamps is a good choice, but a raw item’s arrival time is not necessarily the time its derived features became available. The current design makes `first_seen_at` the governing retrieval key. [^signals-192-195]

Suppose a report arrives at 10:00, extraction finishes at 10:02, and verification finishes at 10:05. A simulated decision at 10:01 must not use the verified signal simply because its underlying article was already present.

I would distinguish:

**Event time; claimed publication time; collector observation time; artifact/version creation time; and available-for-decision time.**

Every feature used at decision time $T$ should satisfy the availability rules for both its raw inputs and its transformations. Cluster assignments and source-reliability estimates need the same treatment.

### Feast does not make this automatic

The recommendation that Feast directly implements the necessary safeguards is overstated. Current Feast documentation explicitly warns that default point-in-time joins constrain event timestamps but can still return later backfills or corrections. It documents `filter_by_created_timestamp=True` for supported offline stores, with the important assumption that the created timestamp correctly represents availability. [^signals-438-443] ([docs.feast.dev](https://docs.feast.dev/getting-started/concepts/point-in-time-joins))

Use that capability where appropriate, but test the semantics of the pinned version and chosen backend.

A mandatory regression test should be:

> A value observed at 10:00 is corrected at 11:00. A forecast reconstructed for 10:30 still receives the original value.

### Split training by label availability too

The 2005–2014 / 2015–2019 / 2020–2024 split is insufficient by itself for overlapping, multi-year forecasts. A training example created before a cutoff may have an outcome that was not known until after it. [^design-439-454]

Require `label_available_at` to precede the training cutoff, and purge overlapping examples where appropriate. Fit normalisers, feature selection, source scores, calibration, and other learned transformations inside the same temporal discipline.

Also, the system has substantially more than “two free parameters”: reference-class selection, feature weights, coordination assumptions, clustering thresholds, source weights, and market-selection rules all create tuning opportunities. The governance record should cover the complete pipeline.

## 8. Make contract payout and execution central to the design

### Forecast the contract, not just the underlying world event

The design correctly recognises that a true claim may be irrelevant to a market’s resolution rule. However, it also says market binding is recorded rather than enforced. [^signals-305-308] [^design-413-416]

For research forecasts, preserving the original question is appropriate. **For trading, a material mismatch must block new exposure until reviewed.**

Maintain separate objects for the research proposition and the tradeable contract. Bind the latter to the exact outcome token, rules version, deadline with timezone, resolution source, and relevant clarification history.

Polymarket’s current documentation confirms that rules—not just titles—govern resolution, that disputes can escalate through UMA, and that an “Unknown/50-50” outcome can pay $0.50 per token. Therefore, actual payout and settlement status must be recorded separately from the binary research outcome. A position must not disappear from economic evaluation merely because its outcome is excluded from binary Brier scoring. ([docs.polymarket.com](https://docs.polymarket.com/concepts/resolution?utm_source=chatgpt.com))

### Evaluate the price you could actually obtain

A market midpoint is not an executable purchase price. Polymarket exposes separate order-book, price, midpoint, spread, and last-trade data. Your decision and simulation need to preserve the relevant book state and quote freshness. ([docs.polymarket.com](https://docs.polymarket.com/market-data/prices-order-books))

For a simple binary contract held to settlement:

$$
EV_{\text{per share}}
=
p_{\text{settlement}}
-
\text{expected acquisition price}
-
\text{additional costs}.
$$

Use expected acquisition price at the intended size, including book depth and execution assumptions. Do not subtract the spread a second time if it is already incorporated through the acquisition price.

A hypothetical illustration:

> Central estimate: 64%. Conservative estimate accounting for model uncertainty: 60%. Executable acquisition price: 61¢. Additional costs: 1¢. Conservative expected value: **−2¢ per share—abstain.**

This is a more useful operational output than “verified signals favour YES.”

Fees must be retrieved for the applicable market. Current documentation distinguishes fee-bearing categories from fee-free geopolitical/world-event markets, so a blanket fee assumption would be wrong. ([docs.polymarket.com](https://docs.polymarket.com/trading/fees))

### Paper trading should start earlier

Order-book collection and shadow execution should run alongside prospective forecast evaluation, not wait until a lengthy accuracy gate has passed. Otherwise, the project may spend months establishing a small predictive improvement only to discover that it is unavailable after latency and execution costs.

For passive orders, do not assume “the market touched my limit” means a fill. Model queue uncertainty, partial fills, cancellation latency, and the possibility that fills occur disproportionately when the price is becoming unfavourable.

## 9. Fix the live-risk interlock before any trading build

The proposed kill switch relies on `monitor` observing a file, writing a database flag, and other services checking that flag. All writes are then supposed to stop. [^design-525-529]

This leaves important failure cases unspecified.

What happens when the monitor is dead, the database is locked, or the exchange still holds resting orders? Stopping the strategy does not cancel those orders, and switching a process to “paper” does not remove existing financial exposure.

The halt state should mean:

**No new exposure; cancellation and reconciliation remain active; monitoring and audit writes continue; existing positions remain under explicit risk management.**

Use an independently enforced, expiring health permission for order entry rather than relying only on a persistent “healthy” flag. Polymarket documents a heartbeat mechanism that automatically cancels open orders when heartbeats stop; assess and test it as an additional safeguard, not a substitute for reconciliation. ([docs.polymarket.com](https://docs.polymarket.com/api-reference/trade/send-heartbeat))

Position limits should be based on monetary loss scenarios, concentration, liquidity, and uncertainty—not cluster `n_eff`. The document’s proposal to size positions on cluster effective sample size conflates statistical evidence with financial risk. [^design-595-600]

Finally, keep the signer and risk checks outside the LLM’s authority. Retrieved news and documents must remain untrusted input: they should never be able to instruct the system to execute tools, expose secrets, or alter trading controls.

## 10. The architecture and evaluation programme I would use

I would retain the broad separation of ingestion, forecasting, evaluation, and execution, but make the interfaces more explicit.

### Contract registry

This defines the exact target, rules, deadline, payout possibilities, event family, eligibility, and resolution history. Any ambiguity or rule mismatch produces a visible abstention or review state.

### Evidence ledger

This preserves raw artifacts, claims, source lineage, contradictions, corrections, availability times, and verification decisions. Repeated coverage of one observation does not become repeated independent evidence.

### Forecasting layer

Start with one or two clearly defined event families. Produce a baseline, an independent evidence-based forecast, and a market-conditioned candidate. Include time remaining and current procedural state. Enforce logical consistency between related forecasts—for example, an event’s probability of occurring by an earlier deadline cannot exceed its probability by a later deadline.

### Decision and risk layer

This combines the forecast with current executable prices, costs, uncertainty, exposure, and eligibility. Its most common legitimate output may be **no trade**. Every decision should preserve the evidence and assumptions that made it actionable or non-actionable.

I would not require every tool in the ecosystem table at the outset. Start with a small system that demonstrably preserves availability-time correctness and lineage. Add a feature store, orchestration platform, or specialised index when its operational benefit is clear—not as a proxy for statistical rigour.

### Evaluate three separate kinds of success

| Evaluation track | What it should establish |
|---|---|
| **Evidence quality** | Extraction fidelity, correct entity/time/negation handling, source attribution, correction handling, and false corroboration rates |
| **Forecast quality** | Prospective paired performance against matched baselines, calibration by family/horizon, uncertainty, coverage, and signal ablations |
| **Economic quality** | Net results under realistic fills, latency and costs, concentration, drawdown, capacity, and settlement delays |

For forecast evaluation, compare the same contracts at the same information cutoffs. Score a predefined eligible sample, including no-bet decisions; separately report performance on the trading subset. Calibration on a fixed selected subset is meaningful for that subset, but cannot establish whole-universe calibration.

Predeclare evaluation dates or use a justified sequential-testing procedure. Repeatedly checking an ordinary 95% interval and stopping at the first favourable result should not become the live gate.

Most importantly, use ablations:

> Does the model improve when verified signals are added? Does deduplication help? Does source-lineage weighting help? Does the evidence model improve on the contemporaneous market benchmark?

Without these comparisons, you may build an elaborate pipeline without learning which components actually contribute predictive information.

ForecastBench can provide useful external context, but it should not be a trading passmark. Its documentation describes difficulty-adjusted comparisons precisely because question sets differ. Your primary benchmark must remain matched to your own contracts and decision times. ([forecastbench.org](https://www.forecastbench.org/docs/?utm_source=chatgpt.com))

## Recommended order of work

**First, repair the specification.** Remove the probability bands, correct Stage 4, replace the generic signal likelihood ratio with contract-specific effects, and revise the power analysis. Resolve the scope conflict: the original Phase 0 deletes trading unless enough long-dated structural markets exist, whereas the addendum explicitly trades T1/T2 and keeps T3 forecast-only. Those rules cannot both govern the project. [^design-547-556] [^signals-23-29]

**Second, build collection and auditability.** Capture the eligible market universe, rules changes, book snapshots, raw evidence, and availability times. Repair the reproduced schema gaps and establish externally timestamped forecast commitments.

**Third, establish a narrow prospective forecasting experiment.** Use simple baselines, a small number of event families, frozen selection rules, and explicit abstention. Run realistic shadow execution at the same time.

**Only then assess whether a live system is justified and permitted.** Neither the attachments nor the fixture establish an accuracy or profitability advantage yet.

One dependency correction is already warranted: the original `py-clob-client` should not be selected simply because its name was verified. Official migration documentation says legacy V1 clients are unsupported in production; the V2 repository now recommends the unified `Polymarket/py-sdk`, installed as `polymarket-client`, for new projects. Pin and test the chosen release rather than treating name verification as integration verification. ([docs.polymarket.com](https://docs.polymarket.com/v2-migration?utm_source=chatgpt.com))

**Bottom line:** the strongest version of SPINE is not a system that counts verified signals and converts them into confidence. It is a system that preserves evidence, learns how that evidence changes a precisely defined outcome probability, tests whether it adds information beyond the market, and abstains when that advantage cannot survive uncertainty and execution costs.

---

## Source-document references

[^design-263-267]: `SPINE-DESIGN-FINAL.md`, lines 263–267 (uploaded source document; line numbering used in the review).

[^signals-350-356]: `SPINE-SIGNALS.md`, lines 350–356 (uploaded source document; line numbering used in the review).

[^signals-337-360]: `SPINE-SIGNALS.md`, lines 337–360 (uploaded source document; line numbering used in the review).

[^signals-410-421]: `SPINE-SIGNALS.md`, lines 410–421 (uploaded source document; line numbering used in the review).

[^signals-292-303]: `SPINE-SIGNALS.md`, lines 292–303 (uploaded source document; line numbering used in the review).

[^signals-226-238]: `SPINE-SIGNALS.md`, lines 226–238 (uploaded source document; line numbering used in the review).

[^signals-228-234]: `SPINE-SIGNALS.md`, lines 228–234 (uploaded source document; line numbering used in the review).

[^signals-342-355]: `SPINE-SIGNALS.md`, lines 342–355 (uploaded source document; line numbering used in the review).

[^design-64-104]: `SPINE-DESIGN-FINAL.md`, lines 64–104 (uploaded source document; line numbering used in the review).

[^design-79-82]: `SPINE-DESIGN-FINAL.md`, lines 79–82 (uploaded source document; line numbering used in the review).

[^design-494-501]: `SPINE-DESIGN-FINAL.md`, lines 494–501 (uploaded source document; line numbering used in the review).

[^signals-58-88]: `SPINE-SIGNALS.md`, lines 58–88 (uploaded source document; line numbering used in the review).

[^signals-104-106]: `SPINE-SIGNALS.md`, lines 104–106 (uploaded source document; line numbering used in the review).

[^design-470-474]: `SPINE-DESIGN-FINAL.md`, lines 470–474 (uploaded source document; line numbering used in the review).

[^design-456-462]: `SPINE-DESIGN-FINAL.md`, lines 456–462 (uploaded source document; line numbering used in the review).

[^design-215-234]: `SPINE-DESIGN-FINAL.md`, lines 215–234 (uploaded source document; line numbering used in the review).

[^design-418-425]: `SPINE-DESIGN-FINAL.md`, lines 418–425 (uploaded source document; line numbering used in the review).

[^design-241-280]: `SPINE-DESIGN-FINAL.md`, lines 241–280 (uploaded source document; line numbering used in the review).

[^design-323-354]: `SPINE-DESIGN-FINAL.md`, lines 323–354 (uploaded source document; line numbering used in the review).

[^signals-364-427]: `SPINE-SIGNALS.md`, lines 364–427 (uploaded source document; line numbering used in the review).

[^design-374-381]: `SPINE-DESIGN-FINAL.md`, lines 374–381 (uploaded source document; line numbering used in the review).

[^design-374-377]: `SPINE-DESIGN-FINAL.md`, lines 374–377 (uploaded source document; line numbering used in the review).

[^design-348-354]: `SPINE-DESIGN-FINAL.md`, lines 348–354 (uploaded source document; line numbering used in the review).

[^signals-192-195]: `SPINE-SIGNALS.md`, lines 192–195 (uploaded source document; line numbering used in the review).

[^signals-438-443]: `SPINE-SIGNALS.md`, lines 438–443 (uploaded source document; line numbering used in the review).

[^design-439-454]: `SPINE-DESIGN-FINAL.md`, lines 439–454 (uploaded source document; line numbering used in the review).

[^signals-305-308]: `SPINE-SIGNALS.md`, lines 305–308 (uploaded source document; line numbering used in the review).

[^design-413-416]: `SPINE-DESIGN-FINAL.md`, lines 413–416 (uploaded source document; line numbering used in the review).

[^design-525-529]: `SPINE-DESIGN-FINAL.md`, lines 525–529 (uploaded source document; line numbering used in the review).

[^design-595-600]: `SPINE-DESIGN-FINAL.md`, lines 595–600 (uploaded source document; line numbering used in the review).

[^design-547-556]: `SPINE-DESIGN-FINAL.md`, lines 547–556 (uploaded source document; line numbering used in the review).

[^signals-23-29]: `SPINE-SIGNALS.md`, lines 23–29 (uploaded source document; line numbering used in the review).

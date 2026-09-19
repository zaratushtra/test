# SPINE §11 — Tiered Horizons and the Signal Architecture

**Status:** Addendum to SPINE-DESIGN-FINAL. Design only; no code written, nothing benchmarked.
All third-party projects in §11.10 are described from public documentation found via search —
existence and stated purpose only. **No license, maintenance status, or API behaviour has been
verified.** Treat the whole table as Phase 0 audit input.

---

## 11.0 What the tiering changes — and why it rescues the power problem

The revised scope is three tiers:

| Tier | Horizon | Action | Layer / cap |
|---|---|---|---|
| **T1** | 7–14 days | Bet on Polymarket | Layer 3 · 0.60 |
| **T2** | 1–6 months | Bet on Polymarket | Layer 3 → 2 · 0.60–0.70 |
| **T3** | 6 months – 2 years | Forecast only, published | Layer 2 → 1 · 0.70–0.85 |

This is a better design than the previous one, and not for the obvious reason. The obvious reason is
that it bets where it can measure and only publishes where it cannot. The important reason is
arithmetic.

§2 established `n_eff ≳ 24.7 / BSS`, and the previous roadmap put Phase 3 at 18–48 months because
long-dated forecasts resolve slowly. **T1 changes the accumulation rate.** At 7–14 day resolution:

```
15 resolved markets/week, mean pairwise correlation r̄ = 0.10
n_eff per week = 15 / (1 + 14 × 0.10) = 15 / 2.4 ≈ 6.3
n_eff = 250  →  ~40 weeks
```

Under a year to a defensible calibration result, rather than four years. But that arithmetic is
entirely hostage to `r̄`:

```
r̄ = 0.10  →  6.3 n_eff/week   →  ~10 months
r̄ = 0.30  →  2.9 n_eff/week   →  ~21 months
r̄ = 0.60  →  1.7 n_eff/week   →  ~3 years
```

> **Market selection must actively maximise independence.** Diversity across domain, geography and
> causal mechanism becomes a hard selection criterion, not a nicety. Fifteen markets about one
> election are one observation. Fifteen markets spanning a court ruling, a central bank decision, a
> sports outcome, a crypto threshold, a legislative vote and a weather event are close to fifteen.

### 11.0.1 The honest cost of tiering

Three tiers means **three models, not one model at three horizons.**

T1 forecasting — will X happen in ten days — does not use Stage 2 pressure vectors. Five-year debt
trajectories say nothing about a court ruling next Thursday. T1 runs on event-proximate signal:
scheduled calendars, news flow, base rates of similar short events, market microstructure.

The consequence must be stated plainly, because it is the thing most likely to be quietly forgotten:

> **Calibration earned in T1 does not transfer to T3.** Passing the Register A gate on 250
> short-horizon markets demonstrates that the T1 model is calibrated. It says nothing whatever about
> whether the structural method works, because the structural method was not used. T3 remains
> unvalidated on the decade timescale of §2.4.

Publishing a T3 forecast alongside a T1 track record invites exactly the inference SPINE was built to
prevent — spending Layer 3 credibility on Layer 1 claims. Every published T3 forecast must carry the
disclosure that its horizon class has no resolved track record.

---

## 11.1 Organising principle: signal count is not signal evidence

The de-duplication problem and the `n_eff` problem are **the same problem at different ends of the
pipeline.**

Stage 4 exists because twenty forecasts downstream of one thesis are not twenty pieces of evidence.
The identical distortion applies to inputs: fifty articles about one event are not fifty signals, and
five outlets running the same wire copy are not five sources. Counting them as such inflates apparent
corroboration exactly as counting correlated sub-forecasts inflates apparent track record.

So the signal layer reuses the machinery already specified rather than inventing a parallel one:

```
n_eff_sources(cluster) = n / (1 + (n − 1) · r̄_source)      -- Kish, again
```

where `r̄_source` is the mean pairwise correlation among the sources present in that cluster,
estimated from historical co-publication behaviour (§11.4.3). One story carried by AP and forty
syndicating outlets yields `n_eff_sources ≈ 1`. The same story independently reported by Reuters, a
local outlet with its own correspondent, and a primary filing yields ≈ 3.

Everything downstream weights on `n_eff_sources`, never on article count.

---

## 11.2 Signal sources by tier

### T1 — 7 to 14 days

The dominant signal is **scheduled**, not discovered. Most short-horizon resolvable events are on a
calendar somebody publishes.

| Class | Examples | Why it matters at T1 |
|---|---|---|
| Event calendars | Court dates, central bank meetings, earnings, elections, scheduled votes, product launches, sports fixtures | Removes timing uncertainty entirely; the only question is outcome |
| Official feeds | Court dockets (PACER/CourtListener), regulatory filings, agency press offices, legislature order papers | Primary sources; near-zero noise; often the market's actual resolution source |
| News flow | Wire services, beat reporting | Fast but noisy; must go through the full §11.3 pipeline |
| Market microstructure | Order book on the market itself, correlated markets on other venues | Powerful and dangerous — see the reflexivity trap in §11.5.2 |
| Short-horizon base rates | "Given a scheduled X, how often does outcome Y follow within 10 days" | The T1 equivalent of Stage 1; often the single best predictor |

### T2 — 1 to 6 months

| Class | Examples |
|---|---|
| Polling and survey aggregates | Election and approval series, with house effects and vintage tracking |
| Economic releases | Scheduled prints with consensus expectations; surprise vs consensus is the signal |
| Legislative and regulatory pipelines | Bill progress, comment periods, rule-making stages |
| Central bank guidance | Statements, minutes, dot plots, speech tone |
| Physical/alt data | Shipping, satellite, commodity flows, port and energy throughput |
| Conflict event data | ACLED / UCDP for curated incidents; GDELT for narrative volume |

### T3 — 6 months to 2 years

The structural layer already specified in §5.3 — vintage macro, debt trajectories, demography,
energy balance, industrial capacity, alliance dependency. Five- and ten-year windows. No change
from the existing design, except that T3 now never trades.

---

## 11.3 The signal pipeline

Ten stages. Each emits provenance; nothing is discarded silently, because "we filtered it" must be
auditable after the fact.

```
S0  Ingest          raw item + source + first_seen_at (immutable)
S1  Canonicalise    boilerplate strip, text normalise, content_hash
S2  Exact dedup     content_hash collision → drop, record pointer
S3  Near-dup        MinHash/LSH → near-duplicate groups
S4  Event cluster   embeddings + density clustering → event_cluster_id
S5  Syndication     collapse wire copies and same-owner outlets to originator
S6  Independence    compute n_eff_sources(cluster) from co-publication history
S7  Claim extract   structured claim + primary-source binding attempt
S8  Verify          corroboration (independence-weighted), entailment, contradiction
S9  Emit            signal record → point-in-time feature store
```

**S0 is the one that cannot be got wrong.** `first_seen_at` is the moment *we* first observed the
item, stamped once, never revised, never backfilled. Publication timestamps from sources are recorded
separately and treated as claims, not facts — outlets silently re-date articles. Every downstream
point-in-time query keys on `first_seen_at`.

---

## 11.4 Deduplication and source independence

### 11.4.1 Three layers of duplication, three different techniques

| Layer | What it catches | Technique |
|---|---|---|
| Byte-identical | Re-crawls, feed repeats | SHA-256 content hash |
| Near-duplicate | Wire copy with edited headline, truncation, boilerplate drift | MinHash + LSH over shingles (Jaccard ≈ 0.8 threshold) |
| Semantic / same-event | Independent reporting of the same underlying event | Sentence embeddings + density clustering |

These must be separate passes. MinHash will not catch two genuinely different articles about one
event; embedding clustering is too expensive and too fuzzy to be the first line against literal
re-crawls.

### 11.4.2 Event clustering

Documented practice for continuous news streams is embeddings → dimensionality reduction → density
clustering (BERTopic-style: a strong sentence encoder, UMAP, HDBSCAN). Two adaptations are required
for this use case:

1. **Streaming, not batch.** Clusters must accept new members continuously without re-clustering
   history, because re-clustering would retroactively change past feature values and destroy
   point-in-time correctness. Use online/incremental assignment with periodic offline re-fit that
   writes a *new* cluster version rather than mutating the old one.
2. **Time-boxed.** Two events months apart can be textually near-identical ("Fed holds rates"). Cluster
   membership requires temporal proximity as well as semantic similarity.

### 11.4.3 Estimating source correlation

`r̄_source` is learned, not assumed. Build a source × event-cluster incidence matrix over history:

- For each pair of sources, compute co-publication association (φ coefficient or Jaccard over
  clusters both covered, conditioned on cluster size to avoid rewarding sources that cover everything).
- Sources that nearly always co-occur within minutes of each other are syndication partners →
  correlation near 1.
- Feed the pairwise matrix into the Kish formula per cluster.

This also produces a useful by-product: a **publication-lag profile** per source. A source that
consistently publishes an event *first* is a genuinely independent reporter; one that consistently
publishes 40 minutes later is a follower and should be discounted accordingly.

### 11.4.4 Syndication collapse

Before independence weighting, explicitly collapse:

- Known wire originators (AP, Reuters, AFP, PA) and their carriers — detect via byline, dateline,
  `<meta>` attribution, and near-dup grouping to the earliest member.
- Same-owner outlet families — a media group running one story across twelve mastheads is one source.
  Ownership mapping is external data and must be maintained.
- Aggregators and republishers — never counted as sources.

---

## 11.5 Noise removal

### 11.5.1 Volume is not information

News volume about an entity spikes for reasons unrelated to any change in the underlying probability —
anniversary coverage, media cascades, a slow news day, an unrelated story pulling the entity into the
frame. Raw mention counts are among the worst features available and are exactly what naive pipelines
key on.

Normalise: compare cluster volume against a rolling baseline for that entity and topic, and use
**deviation from expected volume** rather than volume. Flag cascade patterns (rapid growth with
`n_eff_sources` staying flat) as amplification, not new information — that signature is precisely one
story being repeated, and it should *reduce* confidence in a volume signal rather than raise it.

### 11.5.2 The reflexivity trap

**Filter news about the market itself.** "Polymarket odds shift on X," "traders now price Y at 68%" —
this is the market talking to itself. Ingesting it as evidence and then updating a forecast that is
compared against that same market creates a feedback loop that will look like signal in backtest and
is nothing. Prediction-market coverage is now common enough that this is a real contaminant, not a
theoretical one.

Maintain an explicit exclusion class: any item whose primary subject is a prediction-market price,
betting odds, or forecast aggregate. Store it — it is useful for measuring attention — but never route
it into the likelihood update.

### 11.5.3 Other filters

| Filter | Rationale |
|---|---|
| Opinion / analysis vs reportage | Commentary restates known facts; it is not new evidence |
| Speculation markers | "could", "may", "reportedly", "sources say" — hedged claims get downweighted, not dropped |
| Recycled and anniversary content | High textual similarity to items >30 days old with no new claim |
| Machine-generated content farms | Low-quality reprints; source reliability prior handles most of it |
| Entity ambiguity | Wrong-"Jordan" errors; require entity linking confidence above threshold |

---

## 11.6 Verification

Four checks, in increasing cost:

1. **Primary-source binding.** Attempt to resolve every claim to an official artifact — a filing, a
   docket entry, an agency release. A claim bound to a primary source needs no corroboration. A claim
   that cannot be bound is a report *about* a fact, not the fact.
2. **Independence-weighted corroboration.** Confidence scales with `n_eff_sources`, never with article
   count. A claim in forty syndicated copies of one wire story is a single-source claim.
3. **Entailment check.** Does the extracted structured claim actually follow from the source text? Cheap
   NLI models catch extraction errors and headline/body mismatch, which is a large category.
4. **Contradiction check against stored claims.** Does this contradict something asserted earlier with
   comparable support? Contradictions do not resolve themselves — they queue for adjudication and both
   claims are held at reduced weight until resolved.

**The resolution-source rule from §5.1 governs everything here.** For a market being traded, the only
verification that ultimately matters is what the market's own resolution source says. A claim can be
true, well-corroborated, and irrelevant because the oracle reads a different document. Signals inform
the forecast; the market's resolution text defines the question.

---

## 11.7 Point-in-time discipline

This is where most backtests of news-driven systems quietly fail, and it is the same failure mode as
revised macro data in §5.3.

Non-negotiable rules:

1. `first_seen_at` is immutable and set once at ingestion.
2. Feature values are never recomputed in place. A model change emits a **new feature version**;
   the old values remain queryable as they were.
3. Every retrieval is a point-in-time join: "what was known as of T," never "what the current table
   says about T."
4. Cluster assignments are versioned (§11.4.2). Re-clustering creates a new version; it never rewrites
   history.
5. Source reliability priors are themselves point-in-time. A source's score in March 2026 must be the
   score as computed from data available in March 2026, not today's score applied retroactively.

Rule 5 is the subtle one and it is routinely violated. If source reliability is learned from outcomes,
applying today's learned reliability to a 2024 backtest leaks outcome information backwards.

**This is a solved problem in MLOps.** Point-in-time correct feature retrieval is the defining feature
of a feature store; see §11.10. Do not hand-roll it.

---

## 11.8 From signals to a forecast

Signals do not set probabilities. They update a prior, in log-odds, with bounded influence.

```
logit(p_t) = logit(p_0) + λ_sig · Σ_c  w_c · LLR_c

w_c   = log(1 + n_eff_sources(c)) / log(1 + N_ref)     -- diminishing corroboration returns
LLR_c ∈ [−L_max, +L_max]                                -- per-cluster influence ceiling
```

Four properties this buys:

- **Corroboration has diminishing returns.** The second independent source moves the estimate a lot;
  the tenth barely. The logarithm encodes that, and it is why `n_eff_sources` must be right.
- **No single story can dominate.** `L_max ≈ 1.5` caps one cluster at roughly a 4.5:1 likelihood ratio.
- **Summation is over clusters, not articles.** Volume cannot substitute for independence.
- **The firewall still binds.** After all updates, the §4 CHECK constraint clamps the result to the
  layer's symmetric band. No quantity of news promotes a T1 claim past 0.60. This is the design's
  existing control doing its job on a new input path, and it is why the signal layer needs no separate
  confidence cap.

`λ_sig` is calibrated exactly as `λ` is in §5.3 — three splits, frozen, versioned on every forecast.
It is a second free parameter and carries the same abuse risk.

---

## 11.9 Schema additions

```sql
CREATE TABLE signal_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash    TEXT NOT NULL,
    source_id       INTEGER NOT NULL REFERENCES sources(id),
    url             TEXT,
    first_seen_at   TIMESTAMP NOT NULL,      -- immutable; the point-in-time key
    claimed_pub_at  TIMESTAMP,               -- source's own claim; a claim, not a fact
    title           TEXT,
    body_ref        TEXT,                    -- path to stored raw text
    item_class      TEXT NOT NULL CHECK (item_class IN
                      ('reportage','opinion','market_commentary','primary_source','other')),
    UNIQUE (content_hash, source_id)
);

CREATE TABLE event_clusters (
    cluster_id       TEXT NOT NULL,
    cluster_version  INTEGER NOT NULL,        -- re-clustering creates versions, never mutates
    created_at       TIMESTAMP NOT NULL,
    window_start     TIMESTAMP NOT NULL,
    window_end       TIMESTAMP NOT NULL,
    PRIMARY KEY (cluster_id, cluster_version)
);

CREATE TABLE cluster_members (
    cluster_id       TEXT NOT NULL,
    cluster_version  INTEGER NOT NULL,
    item_id          INTEGER NOT NULL REFERENCES signal_items(id),
    is_originator    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cluster_id, cluster_version, item_id),
    FOREIGN KEY (cluster_id, cluster_version)
        REFERENCES event_clusters(cluster_id, cluster_version)
);

CREATE TABLE source_correlation (
    source_a       INTEGER NOT NULL REFERENCES sources(id),
    source_b       INTEGER NOT NULL REFERENCES sources(id),
    correlation    REAL NOT NULL CHECK (correlation >= -1 AND correlation <= 1),
    n_observations INTEGER NOT NULL,
    computed_at    TIMESTAMP NOT NULL,        -- point-in-time: never applied retroactively
    PRIMARY KEY (source_a, source_b, computed_at),
    CHECK (source_a < source_b)                -- one row per unordered pair
);

CREATE TABLE signals (                          -- what the forecast pipeline actually consumes
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id        TEXT NOT NULL,
    cluster_version   INTEGER NOT NULL,
    claim             TEXT NOT NULL,
    primary_source_id INTEGER REFERENCES signal_items(id),   -- null if unbound
    n_eff_sources     REAL NOT NULL CHECK (n_eff_sources > 0),
    llr               REAL NOT NULL,
    verification      TEXT NOT NULL CHECK (verification IN
                        ('primary_bound','corroborated','single_source','contradicted','unverified')),
    computed_at       TIMESTAMP NOT NULL,
    feature_version   TEXT NOT NULL,
    FOREIGN KEY (cluster_id, cluster_version)
        REFERENCES event_clusters(cluster_id, cluster_version)
);
```

Note there is no `UPDATE` path for `signals`. Re-evaluation emits a new row with a new
`feature_version`, exactly as `forecast_neff_history` does for `n_eff`.

---

## 11.10 Open source landscape

**Verification status: none.** Every row below is a project that exists and is described as stated in
its public documentation, found via search. Licenses, maintenance activity, and actual API behaviour
are **unverified**. This table is Phase 0 audit input, not a dependency list.

### Adopt in full

| Project | Role | Why |
|---|---|---|
| **Feast** | Point-in-time feature store | Point-in-time joins are its core primitive — it implements §11.7 rules 3–5 directly. Do not hand-roll vintage correctness. |
| **`py-clob-client`** (official; a v2 line is referenced) | Polymarket CLOB access | Polymarket maintains official TS/Python/Rust clients. The Gamma API and parts of CLOB need no auth for read-only market data. |
| **`datasketch`** / **`text-dedup`** | MinHash + LSH near-dup | Standard, well-understood, cheap. `text-dedup` packages MinHash/SimHash/suffix-array pipelines together. |
| **`sentence-transformers`** + **HDBSCAN** (BERTopic pattern) | Event clustering | Documented practice for news story discovery: strong encoder → UMAP → HDBSCAN. Needs the streaming and time-boxing adaptations in §11.4.2. |
| **FAISS** or **hnswlib** / **Qdrant** | ANN index | Required for near-dup lookup at any real ingestion rate. |
| **`fredapi`** + **ALFRED** | Vintage macro for T3 | ALFRED is the vintage archive; the §5.3 calibration depends on it. |
| **`properscoring`** / **`netcal`** | Brier, CRPS, calibration | Don't write scoring code by hand for a project whose credibility is scoring. |
| **Dagster** (or Prefect) | Orchestration with asset lineage | Asset-level lineage maps well onto the audit requirements; either beats cron. |
| **Great Expectations** or **Pandera** | Ingestion validation | Schema and distribution checks at the boundary, where §11 is most fragile. |

### Adopt as data or benchmark

| Project | Role | Why |
|---|---|---|
| **PolyBench** (`github.com/PolyBench/PolyBench`) | Backtest corpus | **The highest-value find here.** Point-in-time cross-sections of ~38,700 binary Polymarket markets over ~5,000 events, each snapshot coupled to CLOB state *and* a temporally aligned news stream. That is close to a purpose-built T1/T2 evaluation corpus, and its contamination controls partly answer the §2.5 objection that historical replay is hindsight — for the *automated* parts of the pipeline. It cannot validate the human-judgment stages. |
| **ForecastBench** | External calibration baseline | Dynamic benchmark, questions gathered daily from nine sources, LLM *and* human forecaster predictions, public leaderboard. Use as the comparator that tells you whether your BSS is impressive or ordinary. |
| **GDELT** | Narrative volume, source diversity | Free, global, ~15-minute cadence, with source URLs and mention counts. **Use it for volume and diversity measurement, not as ground-truth events** — see the caution below. |
| **ACLED** / **UCDP** | Curated conflict events | High precision, human-curated. The appropriate ground truth where they have coverage; GDELT is not. |
| **Media Bias/Fact Check**, **NewsGuard** | Source reliability priors | ~8,000 publishers rated. Useful as a *prior* that your own learned reliability updates away from. NewsGuard is commercial. |

### Adopt the methodology, not the code

| Project | Take |
|---|---|
| **OracleProto** | Knowledge-cutoff and temporal-masking protocol for reproducible LLM forecasting evaluation. The methodology is what matters — it is the discipline that makes any retrospective LLM evaluation non-circular. |
| Source-veracity-from-sharing-behaviour research | The approach of inferring source independence from content-sharing patterns is precisely §11.4.3. Read the method, implement it against your own corpus. |
| **Prediction Arena**, **LEAF**, **Foresight Arena** | Additional comparators and evaluation designs worth reading before finalising the Register A protocol. |

### Do not adopt

| Project | Why not |
|---|---|
| **Polymarket/agents** | An autonomous-trading demo framework. Useful to read for API patterns, but its risk posture is the opposite of §6 — this design requires trading to be a rebuild, not a flag. |
| **GDELT CAMEO event codes as direct features** | Machine-coded from news with well-documented false positives and duplicate coding of single events. Feeding them in raw injects exactly the duplication problem §11.4 exists to solve. A 2026 comparative analysis of GDELT vs POLECAT against ACLED as a white-box benchmark is the relevant prior reading. |
| Generic "news sentiment API" products | Sentiment is a weak, heavily-processed proxy that discards provenance — the one thing this architecture depends on. |

---

## 11.11 Roadmap deltas

| Phase | Change |
|---|---|
| **0** | Add two audits: (a) **market-independence survey** — can the T1 universe supply ~15 weekly markets with `r̄ ≤ 0.1`? This now determines the Phase 3 timeline more than anything else. (b) **PolyBench replication** — pull it, confirm the point-in-time claims hold, decide whether it serves as the backtest corpus. |
| **1** | Signal pipeline S0–S6 built alongside the forecast stages. Feast integration is Phase 1 work, not a later optimisation — retrofitting point-in-time correctness is far harder than starting with it. |
| **2** | Add `λ_sig` calibration on the same three-split protocol as `λ`. Two free parameters now, two freeze points. |
| **3** | Revised to **10–18 months** at `r̄ ≤ 0.1` and 15 markets/week, replacing 18–48. This estimate is entirely dependent on the Phase 0(a) result. |
| **5** | Paper trading becomes T1/T2-specific and gains an execution-realism requirement: short-horizon markets are where slippage is most likely to consume a modest edge. |

---

## 11.12 Risks specific to the signal layer

1. **`r̄` between markets is the whole timeline.** If the T1 universe is dominated by correlated
   political questions, `n_eff` accumulates slowly and Phase 3 stretches back toward years. Measure it
   in Phase 0 before committing.
2. **Reflexivity contamination (§11.5.2)** will look like signal in backtest. If prediction-market
   commentary leaks into the likelihood update, the system learns to read its own reflection and the
   error is invisible until live trading.
3. **Retroactive source scores (§11.7 rule 5)** leak outcomes backwards. Subtle, easy to implement by
   accident, and fatal to any backtest result.
4. **Two calibrated free parameters** (`λ`, `λ_sig`) double the surface for tuning-until-it-looks-right.
   Both need frozen splits and committed configs.
5. **Ingestion cost and rate limits.** Continuous multi-source news ingestion at useful latency is an
   ongoing operational cost, not a one-off build. Budget it in Phase 0.
6. **Legal and terms-of-service constraints on scraping.** Unexamined here. Must be checked before any
   crawler runs.

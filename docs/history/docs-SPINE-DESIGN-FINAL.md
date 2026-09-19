# SPINE — Final Design and Roadmap

**Status:** Canonical. Supersedes DESIGN-R3 and `SPINE_Plan_Rev_v2.md`.
**Nature:** This is a design document. No code has been written. Nothing in it reports a test result,
a build outcome, or a verified external fact. Every dependency and image is listed as unverified in
§9 until someone checks it.

**Provenance:** Built on DESIGN-R3, with the round-3 review findings applied, the controls deleted by
`SPINE_Plan_Rev_v2.md` restored, and one new analysis — the power calculation in §2 — that changes
the roadmap materially.

---

## 0. How to read this

§2 is the section that matters. It answers a question the previous four documents deferred: how many
resolved forecasts does it actually take to demonstrate that this method works? The answer is large
enough that it reorders the roadmap and raises a legitimate question about whether the project should
proceed. Read it before the architecture.

Everything else is conventional: schema in §4, stage specifications in §5, roadmap in §7.

---

## 1. Premise and scope

**This is (a)-with-gates.** A forecast-publishing and evaluation system that *may* later trade, behind
hard gates. The trading subsystem is **dormant, not absent** — it exists in the design, is excluded
from the default build, and cannot be enabled without passing every gate in §7 plus an explicit
governance file.

Earlier revisions claimed trading had been "removed." It was not; it was gated. That claim is retracted
and the architecture is stated honestly here.

**Why not pure trading.** SPINE's own boundary says that where a market prices something, the market is
the better forecast, and the method's niche is the 2–10 year structural question no market trades.
Polymarket is the liquid short-horizon venue the protocol says to defer to. Most of its contracts resolve
in weeks to months — Layer 3, confidence capped at 0.60 — while Stages 1–2 generate signal on 5–20 year
timescales. Trading the bulk of that venue would mean systematically spending Layer 1 confidence on
Layer 3 contracts, the exact failure the confidence firewall exists to prevent.

**Why not pure forecast-publishing.** Because a small population of long-dated markets may exist where
the method both applies and can be tested against a price. Phase 0 determines whether that population is
real. If it isn't, the system stays forecast-only permanently and the trading subsystem is deleted rather
than dormant.

---

## 2. The validation problem, quantified

### 2.1 Why the previous gates were non-functional

Four documents proposed a gate: "BSS > 0 on ≥30 forecasts over 90 days," then "n_eff ≥ 10," then
"n_eff ≥ 40." None was derived from a power analysis. Here is one.

Let `p_b` be the benchmark (base-rate) probability and `p_m` the model's forecast, deviating by `δ`.
Per-forecast Brier difference:

```
d = (p_b − o)² − (p_m − o)²
  = −2δ(p_b − o) − δ²          where δ = p_m − p_b
```

For a calibrated forecaster the expected improvement equals the variance of forecasts around the
benchmark — this is the resolution term of the Murphy decomposition:

```
E[d] = E[δ²]
Var[d] ≈ 4·E[δ²]·p_b(1 − p_b)
```

With `B_base = p_b(1 − p_b)` and `BSS = E[d] / B_base`, the variance-to-effect ratio collapses to a
form independent of the base rate:

```
σ²/Δ² = 4·p_b(1−p_b) / E[δ²] = 4 / BSS
```

For a one-sided test at α = 0.05 with 80% power, `(z_α + z_β)² = (1.645 + 0.842)² = 6.18`:

```
n_eff  ≳  6.18 × 4 / BSS  =  24.7 / BSS
```

### 2.2 The numbers

| True BSS | Required `n_eff` | Interpretation |
|---|---|---|
| 0.02 | ~1,235 | Marginal edge |
| 0.05 | ~494 | Modest, realistic edge |
| 0.10 | ~247 | Strong edge |
| 0.20 | ~124 | Very strong |
| 0.30 | ~82 | Superforecaster territory |
| 0.50 | ~49 | Implausible for geopolitics |

Inverting for the previously proposed gates:

- **`n_eff ≥ 10`** requires **BSS ≈ 2.47** for 80% power. BSS is bounded above by 1.0. This gate cannot
  be passed by any real skill level; any pass would be noise.
- **`n_eff ≥ 40`** requires **BSS ≈ 0.62** — an edge far beyond what published forecasting research
  reports for geopolitical questions.

Both proposed gates are not merely weak. They are incapable of detecting the effect they exist to
detect.

### 2.3 The constraint this creates

`n_eff`, not raw forecast count, is what enters the formula. Stage 4 exists because correlated
forecasts are not independent evidence. Therefore:

> **You cannot solve the power problem by making more forecasts about the same thesis.**

Twenty sub-forecasts downstream of one Iran thesis contribute `n_eff ≈ 1.2`, not 20. Reaching
`n_eff ≈ 250` requires on the order of 250 *genuinely independent* questions — different regions,
different mechanisms, different causal structures. That is the binding constraint on the entire
enterprise, and it is a constraint on subject matter, not on engineering.

### 2.4 Levers, honestly assessed

1. **Accept the timeline.** At a realistic throughput of 2–4 independent structural forecasts per week,
   `n_eff ≈ 250` is 2–4 years of production *before* the resolution horizon is even reached. Combined
   with multi-year deadlines, first verdict is plausibly 5+ years out.
2. **Raise throughput.** Stage 3 (payoff matrices, best-response checks) is the per-forecast bottleneck.
   Partial automation is the highest-leverage engineering work in the project. Measure it in Phase 0.
3. **Claim less.** Only assert an edge large enough to be detectable at achievable `n_eff`. Honest, and
   it makes the claim less interesting.
4. **Paired scoring where a market exists.** Scoring against the market price on the same question is a
   paired design, which removes the variance component from question-difficulty heterogeneity — often
   the dominant term. But the effect size against a market is smaller than against a naive base rate,
   so the net effect on `n` must be modeled, not assumed. Worth modeling in Phase 1; not a guaranteed win.
5. **Narrow the domain.** Pick a class of question where many genuinely independent instances exist
   (e.g. sovereign fiscal stress across many countries) rather than one where everything correlates.
   This is the only lever that raises `n_eff` rather than just raw `n`.

### 2.5 Register A / Register B

**Register A — short-horizon, Layer 3.** Purpose: verify the pipeline runs, data flows, scoring
computes. Gate: `n_eff` per the §2.2 table for the effect size being claimed, cluster-bootstrap 95% CI
lower bound on BSS > 0. **Passing Register A is evidence the plumbing works. It is not evidence of
structural edge**, because nothing resolving inside months is Layer 1.

**Register B — long-horizon, Layer 1/2.** No outcome gate on any human timescale. Interim *process*
metrics only:

- Pre-registration integrity: zero chain-verification failures.
- Reproducibility: every forecast reconstructible from stored inputs (see §5.6 on the LLM exception).
- Calibration floor on the Layer 3 subset: Brier **not worse** than the base rate by more than 2σ.
  (Stated one-sided deliberately — a two-sided "within 2σ of base rate" band would fail a system that
  genuinely beats the benchmark.)

> **The Layer 1 structural claim remains unvalidated, and will remain so until enough long-dated
> forecasts resolve — realistically a decade.** No document produced by this project may describe it
> otherwise.

### 2.6 Historical replay is a smoke test

Replaying historical markets against forecasts generated now is hindsight, not pre-registration. Stage 1
reference-class selection and Stage 3 incentive analysis are human-judgment steps that cannot be blinded
to post-T information in practice. Backtest is plumbing verification only. It never gates credibility.

---

## 3. Architecture

Four services. Three run by default; the fourth is absent unless explicitly enabled.

| Service | Role | Default |
|---|---|---|
| `etl` | Pulls structural indicators and market metadata; writes vintage-tagged rows | running |
| `forecast-api` | Stage 0–4 pipeline; registers forecasts; serves `/forecast` | running |
| `monitor` | Resolution capture, scoring, `/metrics`, `/health` | running |
| `order_manager` | Trading. Not built into the default image. | **absent** |

Each service is a separate container from one image, with `tini` as PID 1, `exec`-ed so it receives
SIGTERM directly, and its own `asyncio.run()`. They communicate through SQLite on the mounted volume.
No socket IPC — it adds coupling with no purpose.

**SQLite concurrency is a real risk with three writers.** Set `PRAGMA journal_mode=WAL` and
`PRAGMA busy_timeout=5000` on every connection. WAL is unreliable on network filesystems; the data
volume must be local disk. `database is locked` is the most likely runtime failure in this design.

---

## 4. Data model

Complete schema. Probability is stored in **basis points as INTEGER** to avoid float comparison failures
against the cap constraints.

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ============================================================
-- Vintage-tagged structural indicators (Stage 2 input)
-- The as_of / published_at split is the anti-lookahead control.
-- ============================================================
CREATE TABLE raw_indicators (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,        -- 'ALFRED', 'IMF', 'EIA', ...
    indicator     TEXT NOT NULL,
    country       TEXT,
    value         REAL NOT NULL,
    as_of         DATE NOT NULL,        -- date the observation REFERS TO
    published_at  DATE NOT NULL,        -- date the value BECAME PUBLIC
    vintage_id    TEXT,                 -- publisher's vintage identifier, when available
    ingested_at   TIMESTAMP NOT NULL,
    UNIQUE (source, indicator, country, as_of, published_at)
);
CREATE INDEX idx_indicators_lookup
    ON raw_indicators (indicator, country, published_at, as_of);

-- ============================================================
-- Reference classes (Stage 1) — frozen before the case is examined
-- ============================================================
CREATE TABLE reference_classes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL,
    n            INTEGER NOT NULL CHECK (n >= 0),
    k            INTEGER NOT NULL CHECK (k >= 0 AND k <= n),
    alpha        REAL NOT NULL DEFAULT 1.0,
    beta         REAL NOT NULL DEFAULT 3.0,
    frozen_at    TIMESTAMP NOT NULL,     -- NOT NULL: the freeze is the control
    freeze_note  TEXT NOT NULL
);

CREATE TABLE reference_class_members (
    class_id    INTEGER NOT NULL REFERENCES reference_classes(id),
    event_name  TEXT NOT NULL,
    event_date  DATE NOT NULL,          -- when the historical event OCCURRED
    outcome     INTEGER NOT NULL CHECK (outcome IN (0, 1)),
    added_at    TIMESTAMP NOT NULL,     -- audit: when it entered the class
    source_url  TEXT,
    PRIMARY KEY (class_id, event_name)
);

-- ============================================================
-- Forecasts: the immutable pre-registration commitment.
-- Nothing discovered or recomputed later lives here.
-- ============================================================
CREATE TABLE forecasts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_hash          TEXT NOT NULL UNIQUE,
    prev_hash              TEXT NOT NULL UNIQUE,   -- genesis = 64 zeros
    event                  TEXT NOT NULL,
    resolution_criterion   TEXT NOT NULL,
    deadline               DATE NOT NULL,
    layer                  INTEGER NOT NULL CHECK (layer IN (1, 2, 3)),
    probability_bp         INTEGER NOT NULL,
    base_probability_bp    INTEGER NOT NULL,       -- Stage 1 p0
    reference_class_id     INTEGER NOT NULL REFERENCES reference_classes(id),
    pressure               REAL,                   -- Stage 2 P
    lambda_version         TEXT NOT NULL,          -- which frozen lambda produced this
    incentive_factor       REAL,                   -- Stage 3 rho^(N-1)
    admission_record       TEXT NOT NULL,          -- Stage 0 AdmissionRecord, canonical JSON
    market_id              TEXT,                   -- nullable: no market need exist
    market_resolution_text TEXT,
    market_resolution_hash TEXT,
    created_at             TIMESTAMP NOT NULL,
    source                 TEXT NOT NULL,          -- 'human' | 'llm-assisted'
    source_detail          TEXT,                   -- model id + prompt version when llm-assisted

    -- Firewall: symmetric around 0.5. Caps BOTH tails.
    -- A Layer 3 forecast of 0.02 is a 98% claim on the negation.
    CHECK (probability_bp > 0 AND probability_bp < 10000),
    CHECK (abs(probability_bp - 5000) <=
           CASE layer WHEN 1 THEN 3500 WHEN 2 THEN 2000 ELSE 1000 END)
);

-- ============================================================
-- DAG for Stage 4. Correlation has NO default: it must be stated.
-- ============================================================
CREATE TABLE forecast_edges (
    parent_hash      TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    child_hash       TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    conditional_prob REAL NOT NULL CHECK (conditional_prob > 0 AND conditional_prob < 1),
    correlation      REAL NOT NULL CHECK (correlation >= -1 AND correlation <= 1),
    created_at       TIMESTAMP NOT NULL,
    PRIMARY KEY (parent_hash, child_hash),
    CHECK (parent_hash <> child_hash)      -- no self-edges
);

-- ============================================================
-- Append-only logs for everything discovered after registration
-- ============================================================
CREATE TABLE forecast_adjudications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_hash        TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    divergence_flag      INTEGER NOT NULL CHECK (divergence_flag IN (0, 1)),
    market_text_at_check TEXT,
    market_hash_at_check TEXT,
    adjudication_note    TEXT NOT NULL,
    adjudicator          TEXT NOT NULL,
    checked_at           TIMESTAMP NOT NULL
);

CREATE TABLE forecast_neff_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_hash TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    effective_n   REAL NOT NULL CHECK (effective_n > 0),
    cluster_id    TEXT NOT NULL,
    dag_version   INTEGER NOT NULL,
    computed_at   TIMESTAMP NOT NULL
);

-- ============================================================
-- Outcomes. Brier is NULL for anything not scored.
-- ============================================================
CREATE TABLE metrics (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id          INTEGER NOT NULL UNIQUE REFERENCES forecasts(id),
    outcome              TEXT NOT NULL CHECK (outcome IN
                            ('resolved_yes','resolved_no','void','disputed','unresolvable')),
    brier                REAL,          -- NULL when outcome is not scorable
    resolution_timestamp TIMESTAMP,
    resolution_source    TEXT NOT NULL,
    processed_at         TIMESTAMP NOT NULL,
    scoring_note         TEXT,
    CHECK ((outcome IN ('resolved_yes','resolved_no')) = (brier IS NOT NULL))
);
```

### 4.1 Integrity triggers

```sql
-- Forecasts are immutable. Full stop.
CREATE TRIGGER forecasts_no_update BEFORE UPDATE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts is immutable; write to an adjudication log'); END;

CREATE TRIGGER forecasts_no_delete BEFORE DELETE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts is immutable; records are never deleted'); END;

-- The chain must actually chain. Without this, prev_hash is decorative.
CREATE TRIGGER forecasts_chain_head BEFORE INSERT ON forecasts
FOR EACH ROW WHEN (SELECT COUNT(*) FROM forecasts) > 0
BEGIN
    SELECT RAISE(ABORT, 'prev_hash does not match current chain head')
    WHERE NEW.prev_hash <> (SELECT forecast_hash FROM forecasts ORDER BY id DESC LIMIT 1);
END;

CREATE TRIGGER forecasts_chain_genesis BEFORE INSERT ON forecasts
FOR EACH ROW WHEN (SELECT COUNT(*) FROM forecasts) = 0
BEGIN
    SELECT RAISE(ABORT, 'genesis record must use the genesis sentinel')
    WHERE NEW.prev_hash <> '0000000000000000000000000000000000000000000000000000000000000000';
END;

-- The reference class must have been frozen BEFORE the forecast was made.
CREATE TRIGGER forecasts_class_frozen_first BEFORE INSERT ON forecasts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'reference class was not frozen before forecast creation')
    WHERE (SELECT frozen_at FROM reference_classes WHERE id = NEW.reference_class_id)
          >= NEW.created_at;
END;
```

`prev_hash` is `UNIQUE` and `NOT NULL` with a genesis sentinel rather than `NULL`, because SQLite
permits multiple `NULL`s in a unique column — which would allow the chain to fork.

### 4.2 Canonicalization

Hashes are worthless if two implementations serialize differently. Canonical form is **RFC 8785 (JSON
Canonicalization Scheme)**:

- UTF-8, no BOM.
- Object keys sorted by UTF-16 code unit.
- No insignificant whitespace.
- Numbers in shortest round-trip form. Probabilities are integers (basis points), which sidesteps float
  formatting entirely.
- Timestamps as ISO-8601 UTC with exactly millisecond precision and a `Z` suffix.

```
content     = JCS({event, resolution_criterion, deadline, layer, probability_bp,
                   base_probability_bp, reference_class_id, lambda_version,
                   admission_record, market_id, market_resolution_hash,
                   created_at, source, source_detail})
forecast_hash = SHA256( content || prev_hash )       -- hex, lowercase
```

`created_at` is inside the hash, so backdating breaks verification. `probability_bp` is the *final*
published value, not a pre-cap intermediate — you commit to what you publish.

A golden vector (fixed input → known digest) lives in `tests/test_schema.py` so any reimplementation can
be checked against it.

---

## 5. Stage specifications

### 5.1 Stage 0 — Admission

Not a regex. A structured checklist producing an auditable record stored with the forecast.

```python
@dataclass(frozen=True)
class AdmissionRecord:
    admitted: bool
    counterfactual_observable: str | None   # what would be seen if this is false
    adjudicator: str | None                 # who can check it, without asking us
    deadline_unambiguous: bool
    resolution_source: str | None           # market id, agency publication, etc.
    reasons: list[str]
    reviewer: str                           # 'human:<name>' | 'llm:<model>@<prompt_version>'
    timestamp: str                          # ISO-8601 UTC
```

All four substantive checks must pass: a counterfactual observable is named; a disinterested adjudicator
is identified; the deadline is a specific date; a named verifiable resolution source exists. Failure on
any one means the claim is logged as commentary and never enters the track record — it can never be a
hit either.

**Market binding is recorded, not enforced.** When a market exists, store `market_resolution_text` and
its hash. Divergence from our criterion writes a row to `forecast_adjudications` and requires human
adjudication. It **never auto-disqualifies** a standing pre-registered forecast — Polymarket amends
resolution text post-listing, and a benign clarification must not void the record.

### 5.2 Stage 1 — Structural base rate

```
p0 = (k + α) / (n + α + β)          Beta(1, 3) default for rare-event classes
```

The class is selected and `frozen_at` is stamped **before** the case is examined; the trigger in §4.1
enforces the ordering. Members carry both `event_date` and `added_at` so retroactive additions are
visible.

### 5.3 Stage 2 — Pressure vectors

```
P            = Σ wᵢ · z(dxᵢ/dt)              standardized trends, not levels
logit(p1)    = logit(p0) + λ · P
```

**Window:** 5 years default; 10 years for debt trajectories and demography, which move on decades.
A 12-month window — as proposed in an earlier revision — reads short-run fluctuation as structural
pressure and is rejected by a property test.

**λ calibration, three splits.** The previous procedure grid-searched λ to minimize Brier *on the
holdout*, which makes the holdout training data and biases the reported performance. Correct procedure:

| Split | Period | Use |
|---|---|---|
| Fit | 2005-01-01 – 2014-12-31 | Feature construction, weight estimation |
| Validation | 2015-01-01 – 2019-12-31 | Grid search λ ∈ [0, 2] step 0.05 |
| Test | 2020-01-01 – 2024-12-31 | **Untouched** until λ is frozen. Examined once. |

λ and its freeze date are committed to `spine_config/lambda_frozen.yaml`. Re-fitting requires a
documented governance decision and a new `lambda_version`, which is recorded on every forecast so old
forecasts remain attributable to the parameter that produced them.

**Feasibility risk:** all of this assumes vintage series exist for the calibration window. US macro is
well covered by ALFRED; IMF, World Bank and EIA vintage coverage is uneven. **Phase 0 audits this**
rather than discovering the gap mid-calibration.

### 5.4 Stage 3 — Incentive equilibrium

Per actor: choice set, payoff ordering, information state. Check whether the predicted action is a best
response, and state which payoff ordering would make it not one.

```
p ← p · ρ^(N−1)        N = actors who must coordinate without enforcement; ρ ≈ 0.7
```

**This stage is the throughput bottleneck of the entire project.** §2.3 requires on the order of 250
independent forecasts; if Stage 3 costs four hours of analyst time each, that is 1,000 hours before the
resolution horizon is even reached. Phase 0 measures the real per-forecast cost. Partial automation here
is the highest-leverage engineering work available.

### 5.5 Stage 4 — Node independence

```
P(B)   = P(B|A) · P(A)
n_eff  = n / (1 + (n−1)·r̄)              Kish design effect
```

`correlation` has **no default value**. An unestimated edge would otherwise default to 0, which means
"independent," which inflates `n_eff` — the precise bias this stage exists to remove. The schema forces
an explicit number.

`n_eff` is recomputed whenever the DAG changes; each recomputation appends to `forecast_neff_history`
rather than mutating the forecast.

### 5.6 Stage 5 — Scoring

Per-forecast Brier only. **BSS is a property of a set and is never stored per row.**

```
B    = mean((pᵢ − oᵢ)²)
B    = reliability − resolution + uncertainty       Murphy decomposition
BSS  = 1 − B_model / B_base
```

**Cluster bootstrap, not naive bootstrap.** Resampling individual forecasts assumes independence — the
same violation Stage 4 exists to correct, reappearing inside the gate Stage 4 feeds:

1. Partition resolved forecasts into correlation clusters from the DAG.
2. Resample **whole clusters** with replacement, to the original cluster count.
3. Recompute BSS per resample. 10,000 resamples.
4. Report the 2.5th percentile as the lower bound. The gate uses the lower bound; the point estimate is
   reported but never gates.

`void` outcomes are excluded from scoring, not counted as misses. `disputed` blocks scoring until
adjudicated. `unresolvable` is excluded and counted separately as a Stage 0 quality signal — a rising
`unresolvable` rate means admission is too lax.

**Reproducibility and the LLM exception.** Register B requires forecasts be reconstructible from stored
inputs. That is achievable for human-sourced forecasts. For `llm-assisted` sources it is not — inference
endpoints drift, retire, and rarely guarantee determinism. Reproducibility is therefore defined as
**auditable**: inputs, prompt version, model identifier and raw output are all stored, so a reviewer can
verify what was done even when it cannot be re-run bit-for-bit. Forecasts marked `llm-assisted` are
reported as a separate stratum in every calibration summary.

---

## 6. Safety defaults

- `SPINE_MODE=paper` is the default and the only mode in the default image. Live requires
  `SPINE_MODE=live` **and** the governance file `/data/go_live` — two controls, not three. Adding more
  flags adds mistake surface, not safety.
- `order_manager` is **not built into the default image**. Enabling trading is a rebuild, not a flag flip.
- Secrets are read-only file mounts under `/run/secrets/`, read once at startup, never exported to the
  environment. The default image mounts only the read-only market-data key. No CLOB credential is present
  unless trading is explicitly built in.
- Kill switch: `monitor` polls `/data/KILL` every 5 seconds. On presence it sets `run_state.halted=1`;
  every service checks that flag before each unit of work and before any write. In-flight work completes
  or rolls back; nothing new starts.
- Health failure forces paper mode. `forecast-api` returns 503; a trading service, if built, refuses to
  start. The health check writes `run_state.health_ok` — it is an interlock, never a log line.
- Clock: NTP is synced **on the host**. The container only *measures* drift against an external source
  and fails its health check beyond 2 seconds. NTP inside a container needs `CAP_SYS_TIME` and fights
  the host kernel clock.
- Container logging to stdout with `PYTHONUNBUFFERED=1`, JSON-structured, Docker rotation at
  `max-size=10m max-file=5`. No `nohup`.

---

## 7. Roadmap

Every gate must pass before the next phase begins. Phase 0 can end the project, which is the point of
putting it first.

### Phase 0 — Feasibility (2 weeks)

Three independent audits, any of which can stop the project:

1. **Market screen.** Pull all open Polymarket markets; tag horizon and book depth; emit `zone`
   (`structural` ≥ 2 years & depth ≥ $10k, `contingent` < 6 months & depth ≥ $5k) and `firewall_cap`.
2. **Vintage-data audit.** For every indicator Stage 2 needs, confirm a point-in-time series exists
   covering 2005–2024. Document coverage gaps per source.
3. **Throughput pilot.** Run five forecasts end-to-end by hand. Measure wall-clock time per forecast,
   Stage 3 especially.

**Gate:** vintage coverage sufficient for calibration; measured throughput consistent with reaching the
§2.2 `n_eff` target on an acceptable timeline; and an explicit, documented go/no-go acknowledging §2.
If fewer than 5 structural-zone markets exist, the trading subsystem is **deleted**, not deferred.

### Phase 1 — Core library and schema (4 weeks)

The five stages as a library, the §4 schema with migrations, and the test suite. No API, no trading, no
container.

**Gate:** integrity tests pass — UPDATE aborts, out-of-cap probability rejected **at both tails**, forged
`prev_hash` refused, canonicalization golden vector reproduces the known digest, self-edge rejected,
unfrozen reference class rejected. Plus golden-file tests per stage. Coverage is reported but is not the
gate; behaviour is.

### Phase 2 — Pre-registration engine (3 weeks)

`/forecast`, `/metrics`, `/health`. Resolution capture. Cluster-bootstrap scoring. Backtest runner,
labelled a smoke test.

**Gate:** forecasts register and verify end-to-end; chain verification passes over a seeded history;
a forecast is demonstrably reconstructible from stored inputs alone.

### Phase 3 — Register A accumulation (18–48 months)

Produce genuinely independent pre-registered forecasts and wait.

**Gate:** cluster-bootstrap 95% CI lower bound on BSS > 0, at the `n_eff` required by §2.2 for the effect
size being claimed.

> **This is the honest headline of the roadmap.** At 2–4 independent forecasts per week, reaching
> `n_eff ≈ 250` takes years, and that is before deadlines resolve. No amount of engineering shortens it.
> Everything before this phase is preparation; this phase is the project.

### Phase 4 — Production container (2 weeks, parallel with Phase 3)

Multi-stage CPU-only build, `tini` PID 1, file secrets, `HEALTHCHECK` (using `urllib`, not `curl` —
`python:3.13-slim` ships no curl), WAL configured, log rotation.

**Gate:** clean build; `docker stop` produces graceful shutdown; restart leaves the chain verifiable with
no duplicate forecasts.

### Phase 5 — Paper trading (optional, 3 weeks)

Only if Phase 0 found a real structural-zone market population. Order-book snapshots, fees and slippage,
position sizing on **cluster `n_eff`** rather than position count.

**Gate:** positive simulated P&L after realistic costs; per-cluster exposure ≤ 10% of notional.

### Phase 6 — Live (optional, ongoing)

Requires a rebuild including `order_manager`, Phase 3 and Phase 5 both passed, the governance file, and
minimum position sizing with hard daily-loss and open-notional caps enforced in code. Automatic reversion
to paper on breach or calibration decay. Deterministic client order IDs; reconciliation against the
exchange before the strategy loop is permitted to run.

---

## 8. What could kill this

Stated plainly, so nobody is surprised later:

1. **The power requirement.** §2 may simply make the validation unaffordable in time. This is the most
   likely project-ending finding and it is knowable in Phase 0.
2. **Correlation.** If the questions the method can address are all downstream of a handful of theses,
   `n_eff` saturates and no amount of output produces evidence.
3. **Vintage data gaps.** If point-in-time series don't exist for the indicators Stage 2 needs, λ cannot
   be calibrated honestly and Stage 2 becomes decorative.
4. **Stage 3 cost.** If a forecast costs a day of analyst time, the timeline in §7 Phase 3 doubles or
   worse.
5. **Reflexivity, if trading is ever enabled.** Thin markets move when you trade them; capacity in the
   qualifying sliver may be too small to matter.

---

## 9. Unverified external facts

Nothing below has been checked. Confirm each before building.

| Item | Status | Note |
|---|---|---|
| Polymarket Python client name/version | **UNVERIFIED** | `py-clob-client` is the one I am aware of. An earlier revision asserted `polymarket-data-client` v0.1.3 "after checking PyPI" — that check did not occur. Verify against PyPI and vendor docs. |
| `python:3.13-slim` digest | **UNVERIFIED** | Pin by full digest, not tag. |
| `ghcr.io/astral-sh/uv` | **UNVERIFIED** | Confirm image path and pin a version. |
| `tini` availability | **UNVERIFIED** | Confirm whether it needs `apt install` on the slim base. |
| Polymarket CLOB endpoints | **UNVERIFIED** | Only relevant if Phase 6 is ever reached. |
| ALFRED / IMF / EIA vintage coverage | **UNVERIFIED** | Phase 0 audit item. Material to §5.3. |
| Polymarket geographic restrictions | **UNVERIFIED** | Relevant before any live deployment. |

All dependencies in the eventual package carry
`# UNVERIFIED — confirm version/digest before build` until checked.

---

## 10. Next artifacts

No further prose revisions. In order:

1. `db/schema.sql` — §4 verbatim, plus migrations.
2. `tests/test_schema.py` — the integrity tests from Phase 1's gate, **unrun**, with no claimed pass
   counts.
3. `spine_config/lambda_frozen.yaml` — the three splits from §5.3, freeze procedure documented.
4. The Phase 0 scripts: market screen, vintage audit, throughput pilot harness.

Items 1 and 2 are mechanical from this document. Item 4 is what actually determines whether the project
proceeds.

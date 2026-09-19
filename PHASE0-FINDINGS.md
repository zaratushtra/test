# SPINE Phase 0 — findings to date

**Date:** 2026-09-19
**Status:** Audit (a) coded and tested against fixtures, not yet run live. Audit (b)
coded, not run. Audit (c) coded and tested. Audit (d) — PolyBench — **complete, and
it overturns a claim I made in §11.10.**

## What could and could not be executed

The authoring sandbox routes outbound HTTPS through an allowlist proxy. Reachability
as measured, not assumed:

| Endpoint | Result |
|---|---|
| `raw.githubusercontent.com` | 200 — PolyBench audit ran for real |
| `pypi.org` | 200 |
| `gamma-api.polymarket.com` | blocked |
| `clob.polymarket.com` | blocked |
| `api.stlouisfed.org` | blocked |
| `api.github.com` | 403 |

So audits (a) and (b) are written to run on Zion and were verified against synthetic
fixtures here. Audit (d) used the one reachable path and is a real result.

---

## 1. PolyBench is not what I said it was — retract the §11.10 entry

I described PolyBench as "the highest-value find," "close to a purpose-built T1/T2
evaluation corpus" whose contamination controls "partly answer the §2.5 objection that
historical replay is hindsight." That was based on search snippets. Reading the actual
repository contradicts it on four counts.

**1.1 It contains no probabilistic forecasts.** The `Prediction` model stores
`decision` (BUY/SELL/HOLD/SKIP), `side` (YES/NO) and `confidence` (0–1). The string
`probability` appears **zero times** in `database/peewee_models.py`. Brier scores and
BSS cannot be computed from a trading action plus a conviction score without inventing
a mapping from `(decision, side, confidence)` to a probability — that would be
fabricated data, not evidence. This alone disqualifies it as a calibration corpus.

**1.2 "Contamination-proof" means something else.** It refers to LLM *knowledge-cutoff*
contamination — the markets are live, so a model has not memorised the outcomes. It is
not a claim about point-in-time feature correctness, which is what §5.3 and §11.7 need.
I conflated the two.

**1.3 There is no market price history.** `Market` is a current-state row keyed by id
with `outcome_prices`, `liquidity`, `volume` and `last_updated DEFAULT CURRENT_TIMESTAMP`
— overwritten in place. Point-in-time state exists **only** as denormalised JSON blobs
inside `Prediction` rows (`order_book_snapshot`, `news_context`), i.e. only at moments
where some LLM happened to make a prediction. You cannot ask "what was the book for
market X at time T"; you can only ask "what did model M see when it looked." That is the
opposite of a vintage archive.

**1.4 The news layer destroys provenance.** `core/news_fetcher.py` queries
`news.google.com/rss/search` per market, decodes the redirect URLs, and scrapes article
bodies (`feedparser`, `google_news_api`, `bs4`). Google News RSS is an algorithmically
ranked aggregate. There is no publisher independence metadata, no ingestion timestamp
distinct from the source's own claim, and no deduplication — so `n_eff_sources` per
§11.4 is uncomputable from it. It gives you the aggregated view, which is precisely
what erases the syndication signal we need.

**Also:** the evaluation discards predictions below `confidence < 0.6` and those without
positive EV. Filtering to high-confidence trades is reasonable for measuring returns and
fatal for measuring calibration, which needs the full distribution including the
uncertain calls.

**Legal:** there is **no LICENSE file** (404). Default is all rights reserved — the code
cannot be reused and the dataset cannot be redistributed without permission. The dataset
itself is not in the repo; it is behind a personal OneDrive share, not an archival host.

**Revised §11.10 entry:**

> **PolyBench** — *Do not adopt as a corpus.* Contains trading decisions, not
> probabilistic forecasts; no price history; news provenance erased by Google News
> aggregation; no license. Read `core/market_data.py` and the CLOB/news alignment
> approach for API patterns. Treat published results as trading-agent evaluation, not
> forecasting calibration.

**One item verified positively:** `py_clob_client` is real and is the client PolyBench
uses. That clears one row of §9's unverified table.

---

## 2. New finding: effective sample size is non-monotonic in basket size

This came out of running the screen, not from theory, and it contradicts an assumption
in §11.0.

`n_eff = k / (1 + (k−1)·r̄)`, but `r̄` is not fixed — it *rises* as you take more than one
market per correlation cluster. Past `k` = number of available clusters, `r̄` grows faster
than `k` does, so evidence per week **falls** while workload climbs.

Measured on the test fixture (20 T1 markets forming 8 clusters):

| basket size | r̄ | n_eff/week | weeks to n_eff=247 |
|---|---|---|---|
| 5 | 0.050 | 4.17 | 59 |
| **8** | **0.050** | **5.93** | **42** ← optimum |
| 10 | 0.083 | 5.71 | 43 |
| 15 | 0.207 | 3.85 | 64 |
| 20 | 0.315 | 2.87 | 86 |

Taking 20 markets per week instead of 8 is **2.5× the work for half the evidence** and
doubles the time to a verdict.

**Implications:**

1. §11.0's "15 markets/week" was arbitrary and, on a cluster structure like this one,
   actively harmful. **The correct weekly basket size is the number of independent
   clusters available** — an empirical quantity the screen now measures and reports.
2. The lever that shortens Register A is **not** trading more markets. It is finding
   more *distinct* clusters, which is a domain-coverage problem: more regions, more
   mechanisms, more question types. Volume within a domain is worthless.
3. This should go into the design as a correction to §11.0 and as a selection rule.

`screen_markets.py` now sweeps basket size and reports the optimum, because the naive
"more is better" intuition is wrong here in a way that costs months.

---

## 3. Audit status

| Audit | State | Gate |
|---|---|---|
| (a) Market screen + independence | Coded, fixture-tested, **needs live run on Zion** | undetermined |
| (b) Vintage coverage | Coded, **needs FRED key + live run** | undetermined |
| (c) Throughput pilot | Harness coded and tested; **needs 5 real forecasts timed** | undetermined |
| (d) PolyBench replication | **Complete** | **FAIL — do not adopt** |

Nothing here yet justifies a go/no-go on the project. Audit (a) is the one that matters
most, and it is a single command once the Gamma API is reachable.

## 4. Next actions

1. Run `screen_markets.py --dump-schema` on Zion, fix `FIELD_CANDIDATES` if extraction
   warns, then run the full screen. **This is the gate.**
2. Get a FRED key, replace `DEFAULT_SERIES` with the real Stage 2 indicator list, run
   the vintage audit. Expect `PARTIAL` — the non-US indicators have no vintage archive,
   which constrains what Stage 2 can honestly claim.
3. Time five real forecasts with `throughput.py`. The Stage 3 share is the number that
   decides whether automation is mandatory.
4. Apply the §11.0 correction from finding 2 and the §11.10 retraction from finding 1
   to the design documents.

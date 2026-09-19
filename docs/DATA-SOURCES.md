# Data Sources and Access

**Constraint:** free and open-source only. **Verified 19 Sep 2026** — access terms change, so re-check
before relying on any row.

The constraint costs less than it might appear. §11.2 of the superseded signals design already
argued that T1's dominant signal is *scheduled, not discovered* — court dates, agency releases,
central bank meetings, legislative calendars. Those are overwhelmingly free government APIs, and
they are better inputs than paid news anyway: primary sources carry near-zero noise and are often the
market's own resolution source. The free constraint pushes toward the sources the design already
preferred.

---

## 1. No registration, no key, no account

Everything on the Phase 0 critical path is here.

| Source | Use | Access |
|---|---|---|
| **Polymarket Gamma API** | Market screen, contract registry, event/market metadata | `https://gamma-api.polymarket.com` — **public, no auth, no key, no wallet** |
| **Polymarket CLOB (read)** | Order books, prices, midpoints, trades — the shadow-execution inputs | Read endpoints public. Only *order placement* needs wallet-signed auth |
| **SEC EDGAR** | Filings; resolution source for corporate-event markets | Free, no key. Requires a descriptive `User-Agent` header identifying the caller |
| **Federal Register** | Rule-making, agency notices; scheduled-event calendar | Public API, no key |
| **GDELT** | Narrative volume and source diversity **only** — not ground-truth events | Free, no key. See the caution in §4 |

**This means Phase 0 is unblocked by nothing but network access.** No account, no form, no email.
Running `phase0/screen_markets.py --dump-schema --limit 5` from any host that can reach the internet
is the entire prerequisite.

---

## 2. Free, but needs a key

None of these block Phase 0. Register when the phase that needs them arrives.

| Source | Needed for | Details asked at signup | Priority |
|---|---|---|---|
| **FRED / ALFRED** | Stage 2 vintage macro (`phase0/audit_vintage.py`) | Email, password, name | **Low — see below** |
| **CourtListener** (RECAP) | Court dockets; resolution source for legal-outcome markets | Email, password | Medium — high value for T1 |
| **congress.gov** | Bill progress, floor schedules | Name, email | Medium |
| **GovInfo** | Federal publications | Email | Low |

**FRED is lower priority than the roadmap implies.** Stage 2 pressure vectors feed **T3**, and T3 is
the tier that cannot be validated on any human timescale. T1 and T2 — the tiers where validation is
even possible — do not use them. The vintage audit is still worth running, but it is not on the
critical path to a first scored forecast.

---

## 3. Registration hygiene

- **Use a dedicated email alias**, not a personal address, for anything that might later sit near
  money. A `+tag` alias is enough for most of these; a separate address is better.
- **Unique passwords per service.** These are low-value accounts individually and a credential-reuse
  vector collectively.
- **Give only what the form requires.** None of the above needs a phone number, address or date of
  birth. If a form asks, that is worth a second look at what the service actually is.
- **No KYC is involved in any row above.** Reading market data from Polymarket requires no identity
  verification. That changes only if trading is ever enabled, which is a separate decision with its
  own jurisdictional gate (§2.1 of the design).

### Keys never enter this conversation

The design already specifies read-only file mounts under `/run/secrets/`, read once at startup and
never exported to the environment. That is not just operational tidiness:

> **Do not paste an API key into chat.** It would be in the transcript permanently. No task in this
> project requires me to see a credential — the scripts read them from the environment or a mounted
> file on your host.

If a script ever appears to need a key handed to it inline, that is a bug in the script.

---

## 4. Dropped or restricted under the free/OSS constraint

| Source | Status | Reason |
|---|---|---|
| **NewsGuard** | **Dropped** | Commercial. Was recommended as a source-reliability prior. |
| Paid news APIs (NewsCatcher and similar) | **Dropped** | Commercial. |
| **Media Bias/Fact Check** | Use with care | Free to read, but check terms before automated collection. |
| **ACLED** | Check the licence | Registration is free, but the licence distinguishes non-commercial from commercial use. If trading is ever enabled this needs reading properly, not assuming. |
| **GDELT CAMEO event codes** | Do not use as features | Unchanged from the earlier audit: machine-coded from news, with documented false positives and duplicate coding of single events. Feeding them in raw injects the exact duplication problem the dedup layer exists to remove. Volume and source-diversity measurement only. |

Losing NewsGuard removes an external reliability prior. That is survivable: the design already
required source independence be *learned* from observed behaviour rather than taken from a vendor
score, and §11.4.3's provenance-graph approach never depended on one.

---

## 5. Open-source libraries

The project is stdlib-only so far and should stay that way as long as it can. When a dependency
becomes necessary, prefer these — all permissively licensed, all previously audited in the design:

`datasketch` / `text-dedup` (MinHash LSH) · `sentence-transformers` + HDBSCAN (event clustering) ·
FAISS or hnswlib (ANN index) · `properscoring` / `netcal` (scoring, though `spine/scoring.py` now
covers what we need) · Feast (point-in-time store, **with `created_timestamp_column` configured** —
see §8.2) · `fredapi`.

**Not** `py_clob_client`: V1 is unsupported on production since CLOB V2 launched. Use
`Polymarket/py-sdk` if a client becomes necessary — though for read-only market data, `urllib`
against the public Gamma endpoints needs no client at all.

---

## 6. Local models

If an LLM is used for claim extraction or Stage 3 assistance, a locally hosted open-weights model
satisfies the constraint and has an additional benefit the design cares about: the prompt, model
version and raw output can all be captured in the forecast manifest without depending on a hosted
endpoint that may drift or retire. §5.6's reproducibility exception exists precisely because hosted
inference cannot be pinned; local weights partly close that gap.

Whatever is used, it is typed `llm_assisted` in the schema and reported as a separate stratum in
every calibration summary.

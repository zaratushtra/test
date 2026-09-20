# Running SPINE

Everything below is **read-only against the venue**. There is no authentication path in this
codebase — `tests/test_venue.py` asserts that the venue client contains no `Authorization` header,
no key handling and no signing code — so no account, wallet or KYC is involved at any point.

## Requirements

Python 3.10+ and SQLite 3.37+ (STRICT tables). No installs, no dependencies.

```bash
python3 run_tests.py        # 18 suites, 914 checks — run this first
python3 run_tests.py --docs # and verify the documentation's own claims
```

## The one thing this project needs from you

The build sandbox has no route to the venue. From any machine with outbound HTTPS:

```bash
python3 run_cycle.py --check
```

Expected on a networked host:

```
  gamma: ok   clob: ok
```

If that passes, the full cycle is one command:

```bash
python3 run_cycle.py --jurisdiction GB --db spine.db --save-dir saved/
```

It screens the market universe, registers contracts, and records an order book for each. `--save-dir`
writes the raw Gamma response so the run can be replayed later — including back in an offline
environment:

```bash
python3 run_cycle.py --from-dir saved/ --db spine.db --jurisdiction GB
```

Useful flags:

| flag | effect |
|---|---|
| `--check` | probe reachability and exit |
| `--zones T1,T2` | which horizon zones to register (default both) |
| `--limit N` | markets to fetch (default 3000) |
| `--no-books` | register contracts only |
| `--from-dir DIR` | replay saved JSON instead of fetching |
| `--sweep` | also run every declared collection query |
| `--record-look` | spend one of the budgeted evaluation looks (irreversible) |

## What a healthy run looks like

```
  zones: T1=142  T2=310  T3_structural=8  excluded=2540
  selected 452 in ['T1', 'T2']
  452 screened, 452 new, 0 already registered, 0 skipped
  registry now holds 452 contracts: 452 close_only
  recorded 448 book snapshots, 4 skipped
  spread: median 240bp, min 100bp, max 1800bp (over 441 two-sided books)
```

Re-running is safe and idempotent on contracts: a market already registered under the same rules
version is not registered again. Books are *not* idempotent, by design — a later capture is new
data, and building the time series is the point.

## Collecting signals

Collection is **anchored to a registered proposition** — items are retrieved *for* a question, not
scraped and sorted afterwards. `docs/EVIDENCE-REPORT.md` §3 is why: two independent reports of one
event share almost no vocabulary, so text similarity cannot group them and must not be asked to.

That means a query needs a proposition first:

```python
from spine import collect, evidence, ledger, registry

con = ledger.connect("spine.db")
src = evidence.ensure_source(con, "Committee Watch", "outlet", owner_group="Meridian")
pid = registry.ensure_proposition(
    con, "The measure reaches a floor vote",
    "A recorded floor vote before the deadline",
    "2026-10-09T00:00:00Z", "committee_adoption", "T1")
collect.declare_query(con, proposition_id=pid, source_id=src,
                      feed_url="https://example.test/rss", query_text="measure")
```

Then `python3 run_cycle.py --sweep` runs every active query each cycle.

Queries are **predeclared and stamped**, for the same reason a reference class is frozen before use:
a query written once you know which articles would have helped is a selection rule fitted to the
outcome. Retire a query by setting `retired_at` rather than deleting it.

A failed fetch is recorded as a run with its reason. A gap in the record that looks like "no news
that day" is worse than a logged failure, because the first is indistinguishable from evidence of
quiet.

## Declaring an evaluation budget

`run_cycle.py` prints a gate verdict every time it runs — which makes every run a **look**, and
`phase1/sequential_peeking.py` measured what unbudgeted looking costs: a weekly check of a fixed 95%
bound turns a ~3% gate into **19.3%** over a year. No bad faith required; the gate is cheap and
someone always looks.

So the gate will not fire at all until a budget is declared:

```python
from spine import evaluate, ledger
con = ledger.connect("spine.db")
evaluate.declare_plan(con, n_looks=10, declared_by="your name",
                      spending="obrien_fleming",
                      note="weekly through the first quarter")
```

The plan is **immutable and undeletable**. Raising the budget after a disappointing look is exactly
the failure it exists to prevent, so the schema refuses it rather than trusting nobody will.

Looks are free until you record one. `run_cycle.py` runs a dry run by default and prints
`[DRY RUN — not recorded]`; `--record-look` spends one, permanently.

What the budget costs is visible immediately. On a record where a plain 95% bound fires at
`[+0.118, +0.259]`, the first of ten O'Brien-Fleming looks demands **z = 6.09** and does not fire.
The thresholds relax across the schedule — 6.09, 4.23, 3.40, 2.95, 2.68, 2.52, 2.42, 2.35, 2.31,
2.28 — so an early stop is rare and meaningful, and almost all power is preserved for the end.

Three things block the gate by name rather than caveating it: no declared plan, fewer than twelve
regimes, and stale scores left by an unprocessed resolution revision.

## Schema versions

The database is stamped with `PRAGMA user_version`, and `ledger.connect()` **refuses** a database
stamped with a different one:

```
spine.db is schema version 2, this code expects 3. Refusing to open it: new code
against old tables returns empty results that read as evidence of absence.
```

`run_cycle.py` exits 3 on this. The schema is still changing and no database holds real data yet, so
the remedy is to recreate. Once there is a record worth keeping, this is where a migration goes —
the refusal exists so that moment is noticed rather than missed.

## Things that mean something is wrong

**`only N/M selected markets carry resolution text`** — Gamma renamed a field. Run
`python3 phase0/screen_markets.py --dump-schema` and fix `FIELD_CANDIDATES` in
`phase0/screen_markets.py`. The registry refuses a market with no settlement rules rather than
inventing one, so a stale key list shows up as mass skipping, not as bad data.

**`crossed book`** — best bid at or above best ask. The two sides were read at different moments or
the feed is malformed. These are refused at the storage layer; a handful is normal, a majority is a
feed problem.

**`reachable but errored`** on `--check` — the endpoint responded and the response was wrong. That is
a venue change, not a network problem, and the two need different responses from you.

## Building the record

Shadow execution needs a **history** of books, so the cycle is meant to run repeatedly. Either cron:

```
0 * * * *  cd /path/to/spine && python3 run_cycle.py --jurisdiction GB --db spine.db >> cycle.log 2>&1
```

or `serve.py`, which is the same thing with the failure modes handled:

```bash
python3 serve.py --interval 3600 --heartbeat health.json \
  -- --jurisdiction GB --db spine.db --sweep
```

It finishes the current pass on SIGTERM rather than aborting mid-write, exits non-zero after
consecutive failures so a supervisor restarts it instead of letting it fail silently forever, kills
a pass that hangs, and writes a heartbeat after each one so a health check reads state rather than
inferring it from the process being alive. A process that is alive and failing every pass is the
case worth catching.

Restarting is safe: contracts are keyed by rules-version hash, items by content hash, manifests by
content, so nothing is re-registered. Books *are* re-recorded, because a later capture is a new
observation — that is the point.

### In a container

```bash
docker build -t spine .
docker run -d --name spine --read-only --tmpfs /tmp \
  -v spine-data:/data spine \
  --interval 3600 --heartbeat /data/health.json \
  -- --jurisdiction GB --db /data/spine.db --sweep
```

The image installs nothing with pip — the project is stdlib-only, so there is no dependency surface
to audit. It runs as an unprivileged user, the code is not writable by that user, `tini` forwards
SIGTERM so a stop is clean rather than a SIGKILL ten seconds later, and the healthcheck reads the
heartbeat rather than liveness.

**There is no secret handling, and no mechanism to add one.** Under the paper-only posture the venue
endpoints are public and `spine/venue.py` has no authentication path at all, so a secrets mount
would be a facility with no use and a liability the moment one appeared.

**Passive fill modelling additionally needs the CLOB trades channel**, which this module does not yet
read. Depth changes alone cannot distinguish a fill from a cancellation
(`docs/SHADOW-EXECUTION-REPORT.md` §2), so until trades are collected, only aggressive execution can
be shadowed honestly.

## What is still owed by a human

None of these are code problems, and all of them get harder to answer once data starts accumulating:

1. ~~The number of looks in the sequential schedule.~~ Now **enforced**: the gate refuses to
   evaluate without a declared plan, and the plan cannot be changed afterwards. You still have to
   choose the number — see *Declaring an evaluation budget* above.
2. **The regime rotation plan** — what actually varies across the twelve regimes. This decides
   whether the bootstrap means anything at all.
3. **One or two event families** to start with, and their frozen selection rules.
4. **`min_width_bp` per horizon class**, and the influence caps in `spine/evidence.py`. Both are
   currently declared policy rather than anything measured.

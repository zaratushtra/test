# Shadow Execution Report — §10.2 and §10.3 implemented

**Date:** 19 Sep 2026
**Status:** **Built and validated on fixtures.** `spine/shadow.py`, 48/48 in
`tests/test_shadow.py`, plus 6 checks in the end-to-end run where the decision layer walks a
*recorded* book.
**What it closes:** the one Phase 2 gate item still marked *partial* — "book walking and fills
exist; needs recorded books" — and the three items §10.2 named and Phase 2 listed as "named in the
design and not yet modelled": queue position, cancellation latency, adverse selection.

Nothing in this module can place an order. Under §2.1's paper-only posture it is the only execution
there is, and it is a measurement apparatus.

---

## 1. Books are artifacts, with the same discipline as signals

Three tables: `book_snapshots`, `shadow_orders`, `shadow_fills`, plus `fill_markouts`.

`book_snapshots` carries the same three-timestamp structure as `signal_items` — the venue's own
timestamp is a claim, `captured_at` is when we saw it, and only `available_for_decision_at` governs
retrieval. `book_at()` keys on the third. Reaching for the book captured *during* a decision is the
single easiest way to manufacture an edge that does not exist, and the retrieval function is the
only place that could do it.

**A crossed book is refused, not repaired.** Best bid at or above best ask means the two sides were
read at different moments or the feed is malformed; the spread and midpoint would both be fictional.
Accepting it would make execution look free at precisely the moments it is most expensive.

`fill_markouts` is a separate table from `shadow_fills` for a structural reason: a markout can only
be computed against a *later* snapshot, and a number that is only knowable later should not appear
writable at the moment of the fill.

---

## 2. Touching a limit is not a fill

The passive model is FIFO and deliberately blunt:

```
available_to_us = max(0, traded_at_level − queue_ahead)
filled          = min(size, available_to_us)
```

Worked through the fixture book (400 resting at 5900):

| traded at the level | filled | why |
|---|---|---|
| 300 | **0** | the level traded, the queue never cleared |
| 500 | 100 | overflow past the queue |
| 900 | 200 (full) | enough volume for the whole order |

**`traded_at_level_usd` is a required argument and is never inferred.** The tempting shortcut is to
read it off the depth at that level shrinking between two snapshots. That inference is wrong:
cancellations and fills are indistinguishable in depth data, so a book that thins out because
everyone pulled their orders would be scored as a full fill. Refusing to guess is most of what this
function contributes.

---

## 3. Intent to cancel is not cancellation

An order stays fillable for the cancellation round trip. `traded_after_cancel_usd` fills — because
the order was live — and is flagged `filled_after_cancel`, because these are the fills that arrive
while the price is going through you. A fill completed before the cancel was sent is not flagged, so
the two populations can be compared rather than conflated.

`shadow_orders` requires a `cancel_at` on every passive order at the schema level. A resting order
with no stated cancellation policy is an unbounded exposure being modelled as a free option.

---

## 4. The §10.2 claim, measured rather than asserted

> "fills arrive disproportionately when the price is moving against you"

That is a testable statement about a record, not an axiom, so `adverse_selection_report()` measures
it. Markouts are signed so **positive means the market moved against the position**, and the report
breaks out passive fills and, within those, post-cancel fills.

On the fixture record the arithmetic lands where the design says it will:

| quantity | value |
|---|---|
| fill rate | 2/3 |
| spread earned by resting inside the mid | **+100bp** |
| markout against the passive fill | **+200bp** |
| **net** | **−100bp** |

A passive fill prints inside the mid — that is what passive execution is for — and then gives the
spread back, with interest, to whatever moved the price. This is a fixture, so the numbers are
arithmetic rather than evidence. What matters is that the apparatus nets the two against each other
instead of reporting the flattering half, and that the same report on real books will either confirm
§10.2's claim or refute it.

If post-cancel fills turn out not to be worse than the rest, the latency model is costing nothing
and should be simplified. If they are much worse, passive execution needs rethinking before any of
the forecasting matters. Either finding is worth more than the assumption it replaces.

---

## 5. One thing the end-to-end run confirmed

The decision layer's own `expected_acquisition_bp` and the shadow module's aggressive fill agree to
floating-point on the same book. They should — both call `walk_book()` — but until the decision
layer was fed a *recorded* book rather than a list built at the call site, nothing had checked that
the price a decision was made against is the price the execution model would produce.

---

## 6. What this does not establish

- **No real book has been recorded.** Every snapshot is a fixture. The CLOB read endpoints need no
  key or wallet (`docs/DATA-SOURCES.md`), so this is blocked on network access alone.
- **`traded_at_level_usd` needs a trade feed**, not a book feed. Depth snapshots alone cannot supply
  it, which is the point of §2 — so passive fill modelling needs the trades channel, and until it
  exists, only aggressive execution can be shadowed honestly.
- **Queue position is modelled as strict FIFO.** Real venues vary — pro-rata, priority tiers, and
  order modifications that reset position. FIFO is the conventional conservative choice for a venue
  whose matching rules are undocumented, but it is an assumption.
- **No latency distribution is fitted.** `cancel_latency_ms` is a per-order input, not a model.

---

## 7. Next

1. **Record real books** from the CLOB read endpoints, and **subscribe to the trades channel** —
   without trades there is no honest passive fill model.
2. **Run the report on real fills** and settle whether post-cancel fills are materially worse.
3. **Fit `min_edge_bp` from measured realised cost** rather than the 100bp placed on judgement in
   Phase 2. This module produces exactly the number that calibration needs.

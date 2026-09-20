# Controls Report — §5.4, §5.5, §8.3, §8.4, §11.1, §11.2, §11.3

**Date:** 20 Sep 2026
**Status:** **Built and validated on fixtures.** Every numbered subsection of the design now has code
behind it or is prose that cannot have any.
**Validation:** `python3 run_tests.py --docs` — 22 suites, 1068 checks, plus 32 documentation checks.

These seven sections had one thing in common: each stated a discipline clearly and none of them had
been implemented. Building them turned up four live bugs, which is the argument for building them
rather than citing them.

---

## 1. What was found on the way

| finding | where | why it was invisible |
|---|---|---|
| A three-day-old book was priced against silently | `shadow.book_at()` | It returned the newest book, and "newest" is not "fresh". Nothing had to fail for this to happen — that is the §11.1 shape exactly. |
| Revoking a lease halted nothing | `lease.revoke()` | Renewal leaves leases overlapping, so ending one leaves another covering the same instant. A failing test, not a review, caught it. |
| No forecast recorded its own configuration | `ledger.register_forecast()` | Five suites broke when the check was added. Every one of them registers forecasts. |
| A fetcher raising anything aborted a whole collection run | `venue.snapshot_books()` | It caught two exception types and promised it caught all of them. An offline replay missing a token raised `KeyError` straight through. |

---

## 2. §11.1 — permission that lapses

> "Order entry is gated by an **expiring health lease**, not a persistent 'healthy' flag: a dead
> monitor cannot clear a flag, but a lease fails safe on its own."

The distinction is easy to read past and is the entire mechanism. A flag needs something alive to
turn it off, and the failure that most urgently needs it turned off is that thing dying.

Three properties, enforced rather than described:

- **A lease cannot be extended.** `UPDATE ... SET expires_at` is refused by trigger. Extending
  converts "this was true a moment ago" into "this is true until further notice" — the flag
  behaviour wearing a lease's clothes. Renewal means granting a new one, which means checking again.
- **A lease states what was checked.** `basis` is required. Without it a lease carries no more
  information than the flag it replaced.
- **Halt and lapse are different events.** `permitted()` distinguishes never-granted, lapsed, and
  halted-by-a-named-person-for-a-reason, because those call for different responses and a bare
  `False` cannot be acted on.

**Halt is not "revoke a lease".** That was the finding: a halt is a statement about permission, so
`halt()` ends *every* lease authorising the scope.

`run_cycle.py --lease-seconds N` closes the loop, and **declines to attest when it cannot**: a pass
that recorded under 80% of its books prints `NOT GRANTING` and whatever lease was live simply lapses.
The basis is the true one —
`cycle completed: 14 contracts registered, 12 of 14 books recorded, 2 skipped`.

---

## 3. §11.2 — sizing, and what it must not use

> "Position limits derive from monetary loss scenarios, concentration, liquidity and uncertainty —
> **not from `n_eff`**, which conflates statistical evidence with financial risk."

`decide()` took `max_notional_usd` as an argument and every caller passed a made-up number, so the
tests checked that *arbitrary* limits are enforced rather than that *correct* ones are computed.

The consequential choice is which side of the book to measure. **Liquidity is measured on the side
you would exit through** — a YES position is closed by selling into the bids — because sizing
against the entry side is a standard way to build a position that is comfortable to open and
impossible to close. Uncertainty is a *reduction only*: a wide interval shrinks the position, a
narrow one never inflates it past the loss budget, since §5.1 puts uncertainty under permission and
not under the estimate.

The binding constraint is reported with the number. "Bound by liquidity" and "bound by loss budget"
call for different responses, and a bare figure hides which you are in.

`n_eff` appears nowhere in the module, and a test checks the module body rather than the docstring
that explains why.

---

## 4. §11.3 — untrusted input

Covered in full in the commit history; the two findings were a feed-supplied `javascript:` URL
stored unvalidated, and bidirectional override characters accepted silently.

The treatment **splits, and the split is the argument**: source text is preserved verbatim and
flagged, because if a publisher emitted an override that is a fact about the publisher and stripping
it destroys evidence — while our own `claims.assertion` is refused outright, because nothing
legitimate needs an invisible reordering control in text we wrote.

---

## 5. §8.4 — the governance record

> "All are researcher degrees of freedom and **all belong in the governance record**."

`spine/params.py` listed them. Listing is not a record. A setting not captured at the moment a
forecast was made is one that can be adjusted afterwards with nothing to show it, and two forecasts
made under different configurations were indistinguishable.

`register_forecast()` now refuses a forecast whose manifest does not commit to a parameter snapshot.
The snapshot carries each parameter's **provenance** alongside its value, which is the part that
matters on re-reading: a number that was `declared` then and is `measured` now is a different
situation from one that never moved.

**23 of 31 parameters are `declared`** — 74% of the tunable surface is choice rather than
measurement. That proportion is the honest headline for a project with no record yet.

---

## 6. §8.3 — the split that a date split does not give you

Two forecasts made the same day, one resolving in a week and one in a year. A naive creation-date
split trains on both, and the second supplies an answer that did not exist at fit time.

Three groups, not two: **train** (before the cutoff and labelled by it), **purged** (before, but
labelled after — not training data, and not test data either since the model has seen the question),
**test** (after the cutoff and after an embargo).

The embargo is there because purging overlaps is not enough: an example created just after the
cutoff was made under the conditions the training labels describe.

**The purge rate is reported and a heavy purge raises.** What survives an 80% purge is a sample
selected by how fast its questions resolved, and a fit on it would look like a fit that worked.

---

## 7. §5.4 and §5.5 — arithmetic got wrong, and arithmetic never justified

**§5.4.** v1's `P(B) = P(B|A)·P(A)` drops the second branch. The error is **one-directional**: every
such probability comes out low by exactly the mass ignored, and the answer still lies in [0,1] and
still looks like a probability, which is how it survived. `marginal()` takes `P(B|¬A)` as a required
positional argument — a default of zero would make v1's form the easy path — and `exclusive()` exists
so that the claim "B cannot occur without A" is written where a reader will find it.

**§5.5.** The coordination penalty is retained only as a labelled subjective assumption *with
mandatory sensitivity testing*. "Mandatory" is enforceable: `coordination_penalty()` returns a band
and has **no attribute yielding a bare adjusted probability**, because a type that can hand back a
point estimate makes the sensitivity optional in practice however firmly the prose asks.

Building it produced the measurement that justifies the suspicion. Base 0.6, ρ over 0.5–0.9:

| actors | centre | band | ratio |
|---|---|---|---|
| 2 | 0.4200 | 0.3000 – 0.5400 | 1.8× |
| 4 | 0.2058 | 0.0750 – 0.4374 | 5.8× |
| 10 | 0.0242 | 0.0012 – 0.2325 | **198×** |

**Note which column misleads.** The *absolute* spread peaks around four actors and then narrows,
because both ends collapse toward zero — so a many-actor estimate looks tightly bounded while ρ moves
it two-hundredfold. A test asserted the spread grows monotonically; it does not, and that is the
finding. `spread_ratio` is the quantity that matters and `decision_sensitive()` answers the only
question worth asking: does the guess decide the outcome?

---

## 8. What this does not establish

- **Every number here is from fixtures.** The controls are validated; nothing has been controlled.
- **The 80% attestation threshold, the fifteen-minute book age, the seven-day embargo and the ρ
  range are all `declared`.** They are in `spine/params.py` with what would replace each.
- **No lease has ever gated a real decision**, because there have been no real decisions.
- **`marginal_over_edges()` reads conditionals nothing has written** outside tests. The edges are a
  recorded judgement, and nobody has made one yet.

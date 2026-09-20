"""
Shadow execution: what the edge looks like after the book gets a say.

v2 §10.3 puts order-book collection and shadow fills *in parallel with*
prospective forecasting rather than after an accuracy gate, on the grounds that
if execution eats the edge you want to know in month one. Until now the project
had `walk_book()` and nothing to walk: `book_snapshot_ref` pointed at a string.

Nothing here can place an order. Under §2.1's paper-only posture this is the
only execution there is, and it is a measurement apparatus.

Three things the design names and v1 never modelled (§10.2):

**Queue position.** "The market touched my limit" is not a fill. A resting order
fills only after the size already queued ahead of it at that price has been
consumed. Modelled FIFO, with the queue and the traded volume both recorded so a
fill rate can be audited instead of believed.

**Cancellation latency.** Intent to cancel is not cancellation. An order stays
fillable for the round trip, and fills inside that window are precisely the ones
that arrive when the price is moving through you.

**Adverse selection.** Measured, not assumed: `markout()` compares the mid at
fill against the mid at a later snapshot, signed so positive means the market
moved against the position. `adverse_selection_report()` then compares filled
orders against all orders, which turns §10.2's claim that "fills arrive
disproportionately when the price is moving against you" into something this
project can confirm or refute on its own data.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .canonical import content_hash
from .decision import BookLevel, Fill, walk_book
from . import timeutil

# One timestamp format for the whole project. spine/timeutil.py has the
# lexicographic-ordering bug that made this non-negotiable; six copies of
# these helpers used to live in six modules and disagreed on whole seconds.
_now = timeutil.now
_iso = timeutil.iso
_parse = timeutil.parse
_canon = timeutil.canonical


class ShadowError(RuntimeError):
    """A shadow execution step would have produced a misleading number."""








# ---------------------------------------------------------------------------
# books
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Book:
    """One side-by-side view of a contract's order book."""
    snapshot_id: int
    contract_id: int
    bids: list[BookLevel]     # descending price: best bid first
    asks: list[BookLevel]     # ascending price: best ask first
    captured_at: str
    available_for_decision_at: str

    @property
    def best_bid_bp(self) -> int | None:
        return self.bids[0].price_bp if self.bids else None

    @property
    def best_ask_bp(self) -> int | None:
        return self.asks[0].price_bp if self.asks else None

    @property
    def mid_bp(self) -> float | None:
        """Midpoint. Useful as a *reference* for markouts and never as a price."""
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price_bp + self.asks[0].price_bp) / 2.0

    @property
    def spread_bp(self) -> int | None:
        if not self.bids or not self.asks:
            return None
        return self.asks[0].price_bp - self.bids[0].price_bp

    def depth_at(self, price_bp: int, side: str) -> float:
        """Resting size at exactly this price — the queue an order joins."""
        levels = self.bids if side == "bid" else self.asks
        return sum(l.size_usd for l in levels if l.price_bp == price_bp)


def _levels(raw) -> list[BookLevel]:
    return [BookLevel(price_bp=int(p), size_usd=float(s)) for p, s in raw]


def record_book(
    con: sqlite3.Connection,
    contract_id: int,
    bids: list[tuple[int, float]],
    asks: list[tuple[int, float]],
    *,
    captured_at: str,
    source: str,
    venue_timestamp: str | None = None,
    capture_lag_seconds: float = 0.0,
    available_for_decision_at: str | None = None,
) -> int:
    """
    Store one book snapshot, content-addressed and point-in-time.

    A crossed book (best bid at or above best ask) is **refused**. It means the
    two sides were read at different moments or the feed is malformed, and the
    resulting spread and midpoint would both be fictional. Silently accepting it
    would make execution look free at exactly the moments it is most expensive.
    """
    b = sorted(_levels(bids), key=lambda l: -l.price_bp)
    a = sorted(_levels(asks), key=lambda l: l.price_bp)
    if any(l.size_usd <= 0 for l in (*b, *a)):
        raise ShadowError("a book level with non-positive size is not a level")
    if b and a and b[0].price_bp >= a[0].price_bp:
        raise ShadowError(
            f"crossed book: best bid {b[0].price_bp} >= best ask {a[0].price_bp}. "
            "The two sides were read at different moments or the feed is "
            "malformed; both spread and midpoint would be fictional")

    cap = _parse(captured_at)
    avail = (_parse(available_for_decision_at) if available_for_decision_at
             else cap + timedelta(seconds=capture_lag_seconds))
    if avail < cap:
        raise ShadowError("a book cannot be usable before it was captured")

    payload = {"bids": [[l.price_bp, l.size_usd] for l in b],
               "asks": [[l.price_bp, l.size_usd] for l in a]}
    h = content_hash(payload)
    # INSERT-then-read, not read-then-INSERT. The obvious idempotence pattern
    # is check-then-act and races: two collectors against one database both see
    # no row and both insert, and one gets an IntegrityError from a function
    # whose whole promise is that a repeat is a no-op. ON CONFLICT DO NOTHING
    # makes the write atomic, so the read afterwards always finds a row.
    con.execute(
        """INSERT INTO book_snapshots
           (contract_id, snapshot_hash, venue_timestamp, captured_at,
            available_for_decision_at, bids, asks, source)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT DO NOTHING""",
        (contract_id, h, venue_timestamp, _iso(cap), _iso(avail),
         json.dumps(payload["bids"]), json.dumps(payload["asks"]), source))
    con.commit()
    return con.execute(
        "SELECT id FROM book_snapshots WHERE contract_id=? AND snapshot_hash=? "
        "AND captured_at=?", (contract_id, h, _iso(cap))).fetchone()[0]


def load_book(con: sqlite3.Connection, snapshot_id: int) -> Book:
    row = con.execute(
        "SELECT id, contract_id, bids, asks, captured_at, available_for_decision_at "
        "FROM book_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise ShadowError(f"no book snapshot {snapshot_id}")
    return Book(row[0], row[1], _levels(json.loads(row[2])),
                _levels(json.loads(row[3])), row[4], row[5])


# A book older than this is not a price, it is a memory. Fifteen minutes is one
# ordinary collection cycle plus slack; past that the collector has missed a
# pass and the number on screen stopped being the market some time ago.
MAX_BOOK_AGE_SECONDS = 900.0


def book_at(con: sqlite3.Connection, contract_id: int, as_of: str,
            max_age_seconds: float | None = MAX_BOOK_AGE_SECONDS) -> Book | None:
    """
    The most recent book a decision at `as_of` could have used, if it is fresh.

    Keys on `available_for_decision_at`, same as every other retrieval here. A
    backtest that reaches for the book captured *during* the decision is the
    single easiest way to manufacture an edge that does not exist.

    **And it refuses a stale one.** This function used to return the newest book
    however old it was, so a collector that died on Friday left Monday pricing
    against Friday's market with no complaint anywhere — the §11.1 failure
    exactly: nothing had to go wrong actively for the system to keep acting on
    something that was no longer true. Pass `max_age_seconds=None` to retrieve a
    stale book deliberately, which is a reasonable thing to want when studying
    the record and never when pricing against it.
    """
    row = con.execute(
        "SELECT id, captured_at FROM book_snapshots WHERE contract_id=? AND "
        "available_for_decision_at <= ? ORDER BY available_for_decision_at DESC, "
        "id DESC LIMIT 1", (contract_id, as_of)).fetchone()
    if not row:
        return None
    if max_age_seconds is not None:
        age = (_parse(as_of) - _parse(row[1])).total_seconds()
        if age > max_age_seconds:
            return None
    return load_book(con, row[0])


def book_age_seconds(con: sqlite3.Connection, contract_id: int,
                     as_of: str) -> float | None:
    """How stale the newest available book is. None when there is none."""
    row = con.execute(
        "SELECT captured_at FROM book_snapshots WHERE contract_id=? AND "
        "available_for_decision_at <= ? ORDER BY available_for_decision_at DESC, "
        "id DESC LIMIT 1", (contract_id, as_of)).fetchone()
    if not row:
        return None
    return (_parse(as_of) - _parse(row[0])).total_seconds()


# ---------------------------------------------------------------------------
# fills
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowFill:
    model: str
    filled_usd: float
    avg_price_bp: float | None
    fully_filled: bool
    queue_ahead_usd: float | None = None
    traded_at_level_usd: float | None = None
    filled_after_cancel: bool = False
    reason: str | None = None

    def summary(self) -> str:
        if self.filled_usd <= 0:
            return f"no fill ({self.reason})"
        s = (f"{self.filled_usd:.2f} @ {self.avg_price_bp:.1f}bp "
             f"({'full' if self.fully_filled else 'partial'}, {self.model})")
        return s + (" AFTER CANCEL" if self.filled_after_cancel else "")


def taking_side(book: Book, side: str) -> list[BookLevel]:
    """The levels an aggressive order consumes. Buying YES lifts asks."""
    if side == "YES":
        return book.asks
    if side == "NO":
        # Buying NO at price p is selling YES at 10000-p: it hits the bids, and
        # the cost in NO terms is 10000 minus the YES price paid.
        return [BookLevel(10000 - l.price_bp, l.size_usd) for l in book.bids]
    raise ShadowError(f"side must be YES or NO, got {side!r}")


def aggressive_fill(book: Book, side: str, size_usd: float,
                    limit_price_bp: int) -> ShadowFill:
    """
    Cross the spread, respecting the limit.

    Levels worse than the limit are simply not available — an aggressive order
    with a limit is still a limit order, and pretending otherwise is how a
    backtest fills at prices nobody offered.
    """
    levels = [l for l in taking_side(book, side) if l.price_bp <= limit_price_bp]
    if not levels:
        return ShadowFill("no_fill", 0.0, None, False,
                          reason=f"no liquidity at or better than {limit_price_bp}bp")
    f: Fill = walk_book(levels, size_usd)
    if f.filled_usd <= 0:
        return ShadowFill("no_fill", 0.0, None, False, reason="no book depth")
    return ShadowFill("book_walk", f.filled_usd, f.expected_price_bp, f.fully_filled)


def passive_fill(
    book: Book,
    side: str,
    size_usd: float,
    limit_price_bp: int,
    traded_at_level_usd: float,
    *,
    traded_after_cancel_usd: float = 0.0,
) -> ShadowFill:
    """
    Rest at a price and wait your turn. FIFO.

        available_to_us = max(0, traded_at_level − queue_ahead)
        filled          = min(size, available_to_us)

    `traded_at_level_usd` must come from **observed trades** and is a required
    argument for that reason. It is tempting to infer it from the depth at that
    level shrinking between two snapshots, and that inference is wrong:
    cancellations and fills are indistinguishable in depth data, so an order
    book that thins out because everyone pulled would be scored as a full fill.
    Refusing to guess is the whole contribution of this function.

    `traded_after_cancel_usd` is volume that arrived during the cancellation
    round trip. It fills, because the order was still live — and it is flagged,
    because these are the fills that arrive when the price is going through you.
    """
    if traded_at_level_usd < 0 or traded_after_cancel_usd < 0:
        raise ShadowError("traded volume cannot be negative")
    resting_side = "bid" if side == "YES" else "ask"
    queue_ahead = book.depth_at(
        limit_price_bp if side == "YES" else 10000 - limit_price_bp, resting_side)

    total_traded = traded_at_level_usd + traded_after_cancel_usd
    available = max(0.0, total_traded - queue_ahead)
    filled = min(size_usd, available)

    if filled <= 0:
        reason = (f"queue: {queue_ahead:.2f} ahead, {total_traded:.2f} traded — "
                  "the level was touched but never cleared")
        return ShadowFill("no_fill", 0.0, None, False, queue_ahead,
                          traded_at_level_usd, reason=reason)

    # Which portion of the fill arrived after the cancel was sent?
    before_cancel = max(0.0, traded_at_level_usd - queue_ahead)
    after_cancel = filled > before_cancel + 1e-9

    return ShadowFill("queue_position", filled, float(limit_price_bp),
                      filled >= size_usd - 1e-9, queue_ahead,
                      traded_at_level_usd, after_cancel)


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------

def record_order(
    con: sqlite3.Connection,
    *,
    contract_id: int,
    book_snapshot_id: int,
    side: str,
    style: str,
    limit_price_bp: int,
    intended_size_usd: float,
    placed_at: str,
    decision_id: int | None = None,
    cancel_at: str | None = None,
    cancel_latency_ms: float = 0.0,
) -> int:
    cur = con.execute(
        """INSERT INTO shadow_orders
           (decision_id, contract_id, book_snapshot_id, side, style,
            limit_price_bp, intended_size_usd, placed_at, cancel_at,
            cancel_latency_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (decision_id, contract_id, book_snapshot_id, side, style, limit_price_bp,
         intended_size_usd, placed_at, cancel_at, cancel_latency_ms))
    con.commit()
    return cur.lastrowid


def record_fill(con: sqlite3.Connection, order_id: int, fill: ShadowFill,
                recorded_at: str | None = None) -> int:
    cur = con.execute(
        """INSERT INTO shadow_fills
           (order_id, fill_model, filled_usd, avg_price_bp, fully_filled,
            queue_ahead_usd, traded_at_level_usd, filled_after_cancel,
            no_fill_reason, recorded_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (order_id, fill.model, fill.filled_usd, fill.avg_price_bp,
         1 if fill.fully_filled else 0, fill.queue_ahead_usd,
         fill.traded_at_level_usd, 1 if fill.filled_after_cancel else 0,
         fill.reason, recorded_at or _now()))
    con.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# markouts: was the fill good, or just available?
# ---------------------------------------------------------------------------

def markout(
    con: sqlite3.Connection,
    fill_id: int,
    reference_snapshot_id: int,
    computed_at: str | None = None,
) -> float:
    """
    Signed adverse selection in basis points. **Positive means it went against us.**

    A fill is only good news if the price does not immediately move through it.
    This is measured against a later snapshot and can therefore never be
    computed at fill time, which is why it lives in its own table: a number that
    is only knowable later should not appear writable at the moment of the fill.
    """
    row = con.execute(
        "SELECT f.filled_usd, o.side, o.book_snapshot_id, f.avg_price_bp "
        "FROM shadow_fills f JOIN shadow_orders o ON o.id=f.order_id "
        "WHERE f.id=?", (fill_id,)).fetchone()
    if not row:
        raise ShadowError(f"no fill {fill_id}")
    filled, side, entry_snap_id, _ = row
    if filled <= 0:
        raise ShadowError("an unfilled order has no markout: there is no position")

    entry, ref = load_book(con, entry_snap_id), load_book(con, reference_snapshot_id)
    if entry.contract_id != ref.contract_id:
        raise ShadowError("markout reference is a different contract's book")
    m0, m1 = entry.mid_bp, ref.mid_bp
    if m0 is None or m1 is None:
        raise ShadowError("a one-sided book has no midpoint to mark against")

    dt = (_parse(ref.captured_at) - _parse(entry.captured_at)).total_seconds()
    if dt <= 0:
        raise ShadowError(
            "the markout reference must be captured after the entry book; "
            "marking against an earlier or simultaneous snapshot measures nothing")

    # Long YES profits when the mid rises, so a fall is adverse. Long NO is the
    # mirror image.
    adverse = (m0 - m1) if side == "YES" else (m1 - m0)
    con.execute(
        """INSERT OR REPLACE INTO fill_markouts
           (fill_id, reference_snapshot_id, horizon_seconds, mid_at_fill_bp,
            mid_at_reference_bp, adverse_bp, computed_at)
           VALUES (?,?,?,?,?,?,?)""",
        (fill_id, reference_snapshot_id, dt, m0, m1, adverse, computed_at or _now()))
    con.commit()
    return adverse


@dataclass(frozen=True)
class AdverseSelectionReport:
    n_orders: int
    n_filled: int
    fill_rate: float
    mean_adverse_bp: float | None
    mean_adverse_passive_bp: float | None
    mean_adverse_after_cancel_bp: float | None
    mean_realised_cost_bp: float | None

    def summary(self) -> str:
        lines = [f"{self.n_filled}/{self.n_orders} orders filled "
                 f"({self.fill_rate:.0%})"]
        if self.mean_adverse_bp is not None:
            lines.append(f"mean markout {self.mean_adverse_bp:+.1f}bp against")
        if self.mean_adverse_passive_bp is not None:
            lines.append(f"passive {self.mean_adverse_passive_bp:+.1f}bp")
        if self.mean_adverse_after_cancel_bp is not None:
            lines.append(f"post-cancel {self.mean_adverse_after_cancel_bp:+.1f}bp")
        return "; ".join(lines)


def adverse_selection_report(con: sqlite3.Connection) -> AdverseSelectionReport:
    """
    Turn §10.2's assertion into a measurement.

    The design claims fills arrive disproportionately when the price is moving
    against you. That is a testable statement about this record, and separating
    passive fills — and within those, the ones that landed during the
    cancellation round trip — is what makes it testable. If post-cancel fills
    are not worse than the rest, the latency model is costing nothing and should
    be simplified; if they are much worse, passive execution needs rethinking
    before any of it matters.
    """
    n_orders = con.execute("SELECT COUNT(*) FROM shadow_orders").fetchone()[0]
    n_filled = con.execute(
        "SELECT COUNT(*) FROM shadow_fills WHERE filled_usd > 0").fetchone()[0]

    def mean(sql, args=()):
        v = con.execute(sql, args).fetchone()[0]
        return float(v) if v is not None else None

    return AdverseSelectionReport(
        n_orders=n_orders,
        n_filled=n_filled,
        fill_rate=(n_filled / n_orders) if n_orders else 0.0,
        mean_adverse_bp=mean("SELECT AVG(adverse_bp) FROM fill_markouts"),
        mean_adverse_passive_bp=mean(
            "SELECT AVG(m.adverse_bp) FROM fill_markouts m "
            "JOIN shadow_fills f ON f.id=m.fill_id "
            "WHERE f.fill_model='queue_position'"),
        mean_adverse_after_cancel_bp=mean(
            "SELECT AVG(m.adverse_bp) FROM fill_markouts m "
            "JOIN shadow_fills f ON f.id=m.fill_id "
            "WHERE f.filled_after_cancel=1"),
        mean_realised_cost_bp=mean(
            "SELECT AVG(f.avg_price_bp - "
            "  (SELECT (json_extract(b.bids,'$[0][0]') + json_extract(b.asks,'$[0][0]'))/2.0 "
            "   FROM book_snapshots b WHERE b.id=o.book_snapshot_id)) "
            "FROM shadow_fills f JOIN shadow_orders o ON o.id=f.order_id "
            "WHERE f.filled_usd > 0 AND o.side='YES'"),
    )

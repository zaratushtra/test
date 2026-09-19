"""
Polymarket access: markets from Gamma, books from the CLOB read endpoints.

Both are public. No key, no account, no wallet — verified 19 Sep 2026 and
recorded in `docs/DATA-SOURCES.md`. Authentication is required only for order
placement, which §2.1 puts out of scope, so nothing in this module can
authenticate and there is no code path that would know how.

Two things here are easy to get wrong and expensive to get wrong quietly.

**Book sizes are in shares, not dollars.** The CLOB returns `size` as a count of
outcome tokens; every consumer in this project (`walk_book`, the queue model)
works in notional USD. At a price of 0.61 the difference is 39% of the depth, in
the direction that makes execution look cheaper than it is. `parse_book()` does
the conversion once, here, and says so.

**An empty response is not an empty book.** A request that returns nothing
because the token is unknown, the market is closed or the endpoint moved looks
identical to a genuinely empty book unless you check. Recording the second as
the first produces snapshots that silently vanish from execution analysis.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from . import shadow

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
USER_AGENT = "spine-venue/0.1"


class VenueError(RuntimeError):
    """A venue call failed, or returned something that must not be trusted."""


class Unreachable(VenueError):
    """The endpoint could not be reached at all. Distinct from a bad response."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _get(url: str, timeout: int = 30) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise VenueError(f"HTTP {e.code} from {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise Unreachable(
            f"cannot reach {url}: {e.reason}. This host has no route to the "
            "venue; run from a machine with outbound network, or pass saved "
            "JSON with --from-dir") from e
    except json.JSONDecodeError as e:
        raise VenueError(f"{url} returned something that is not JSON") from e


# ---------------------------------------------------------------------------
# reachability
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reachability:
    gamma: str
    clob: str

    @property
    def ok(self) -> bool:
        return self.gamma == "ok" and self.clob == "ok"

    def summary(self) -> str:
        return f"gamma: {self.gamma}   clob: {self.clob}"


def check_reachability(timeout: int = 10) -> Reachability:
    """
    Probe both endpoints and report, rather than failing mid-run.

    Worth its own call because the answer is the difference between "the venue
    changed" and "this machine has no network", and those need different
    responses from whoever is reading the output.
    """
    def probe(url):
        try:
            _get(url, timeout=timeout)
            return "ok"
        except Unreachable as e:
            return f"unreachable ({e.__cause__.reason if e.__cause__ else 'no route'})"
        except VenueError as e:
            return f"reachable but errored: {e}"

    return Reachability(probe(f"{GAMMA_BASE}/markets?limit=1"),
                        probe(f"{CLOB_BASE}/ok"))


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------

def fetch_markets(limit: int = 3000, page_size: int = 500,
                  on_page=None) -> list[dict]:
    """
    Page through active, unclosed markets.

    Stops on the first error rather than continuing with a partial universe:
    a market screen run over half the markets would silently understate the
    number of independent clusters, which is the quantity the Phase 0 gate turns
    on.
    """
    out: list[dict] = []
    offset = 0
    while len(out) < limit:
        want = min(page_size, limit - len(out))
        qs = urllib.parse.urlencode({"limit": want, "offset": offset,
                                     "active": "true", "closed": "false"})
        batch = _get(f"{GAMMA_BASE}/markets?{qs}")
        if not isinstance(batch, list):
            raise VenueError(f"expected a list of markets, got {type(batch).__name__}")
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if on_page:
            on_page(len(out))
    return out


# ---------------------------------------------------------------------------
# books
# ---------------------------------------------------------------------------

def parse_book(payload: dict) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    CLOB book payload to `(bids, asks)` as `(price_bp, size_usd)`.

    **Sizes arrive in shares and leave in dollars.** `size_usd = shares × price`.
    Skipping that conversion overstates depth by `1/price − 1` — 64% at 0.61 —
    always in the direction that makes the book look deeper and execution
    cheaper than it is.

    Levels with a non-positive size or a price at or outside (0, 1) are dropped:
    a zero-size level is not a level, and a token priced at exactly 0 or 1 is a
    settled or broken market, not a tradeable one.
    """
    if not isinstance(payload, dict):
        raise VenueError(f"expected a book object, got {type(payload).__name__}")

    def side(key):
        raw = payload.get(key)
        if raw is None:
            raise VenueError(
                f"book payload has no '{key}' key. An absent side is not an "
                "empty side: treat this as a failed read, not a thin book")
        out = []
        for lvl in raw:
            try:
                price = float(lvl["price"]) if isinstance(lvl, dict) else float(lvl[0])
                shares = float(lvl["size"]) if isinstance(lvl, dict) else float(lvl[1])
            except (KeyError, IndexError, TypeError, ValueError) as e:
                raise VenueError(f"unparseable {key} level {lvl!r}") from e
            if shares <= 0 or not (0.0 < price < 1.0):
                continue
            out.append((round(price * 10000), shares * price))
        return out

    return side("bids"), side("asks")


def fetch_book(token_id: str) -> dict:
    """Raw CLOB book for one outcome token. Read-only, unauthenticated."""
    if not token_id:
        raise VenueError("a book request needs an outcome token id")
    return _get(f"{CLOB_BASE}/book?{urllib.parse.urlencode({'token_id': token_id})}")


def snapshot_books(
    con: sqlite3.Connection,
    contracts: list[dict],
    *,
    source: str = "clob_rest",
    capture_lag_seconds: float = 2.0,
    fetcher=None,
    on_result=None,
) -> dict:
    """
    Record a book for each contract. Returns a report; never raises per-contract.

    One bad token must not abort a collection run — but every failure is
    recorded and counted, because a silently shrinking snapshot set is how an
    execution study ends up describing only the liquid, well-behaved half of the
    universe.
    """
    get = fetcher or fetch_book
    recorded, skipped = [], []
    for c in contracts:
        token = c.get("outcome_token_id")
        try:
            if not token:
                raise VenueError("contract has no outcome token id")
            bids, asks = parse_book(get(token))
            if not bids and not asks:
                raise VenueError("both sides empty: no resting liquidity to record")
            sid = shadow.record_book(
                con, c["id"], bids, asks, captured_at=_now(), source=source,
                capture_lag_seconds=capture_lag_seconds)
            recorded.append(sid)
            if on_result:
                on_result(c, sid, None)
        except (VenueError, shadow.ShadowError) as e:
            skipped.append((c.get("market_id") or c.get("id"), str(e)))
            if on_result:
                on_result(c, None, str(e))
    return {"recorded": len(recorded), "skipped": skipped,
            "snapshot_ids": recorded}


def load_saved(path: str) -> object:
    """Read a saved venue response. The offline path for a host without network."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)

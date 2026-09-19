"""
One timestamp format, enforced.

Every point-in-time query in this project is a **string** comparison:
`available_for_decision_at <= ?`, `computed_at <= ?`, `created_at <= ?`. That is
fine, and it is fast, and it is correct — for exactly as long as every timestamp
has the same shape.

It did not. `datetime.isoformat()` omits the fractional part when microseconds
are zero, so a row written at a whole second gets `2026-09-19T13:00:00Z` while
the cutoff it is compared against is `2026-09-19T13:00:00.000Z`. Lexicographic
order puts `'.'` (0x2E) before `'Z'` (0x5A):

    '2026-09-19T13:00:00Z' <= '2026-09-19T13:00:00.000Z'   ->   False

So a forecast recorded at that instant is **invisible to a retrieval at the same
instant**. Not wrong by an hour — wrong by a format, silently, and only for the
rows that happen to land on a whole second. That is the worst possible failure
shape for a discipline the whole project rests on.

Hence: one canonical form, one place that produces it, a parser that accepts the
variants the outside world sends, and a `CHECK` constraint in the schema so a
non-canonical string cannot reach a column that gets compared.

    YYYY-MM-DDTHH:MM:SS.sssZ

UTC always, millisecond precision always, `Z` always.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Mirrors the GLOB in db/schema_v2.sql. Kept as a regex here because Python has
# one and SQLite does not; the two must agree.
CANONICAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

SQL_GLOB = ("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T"
            "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z")


class TimeError(ValueError):
    """A timestamp could not be read, or was not in canonical form."""


def now() -> str:
    """The current instant, canonical."""
    return iso(datetime.now(timezone.utc))


def iso(dt: datetime) -> str:
    """A datetime to the canonical form. Naive input is treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def parse(ts: str) -> datetime:
    """
    Read a timestamp. Accepts what the world sends; `iso()` decides what we store.

    Feeds, venues and humans all produce offsets, missing fractions and lowercase
    `z`. Being liberal here and strict on the way out is the only arrangement
    that keeps the stored record uniform without discarding real data.
    """
    if not isinstance(ts, str) or not ts.strip():
        raise TimeError(f"not a timestamp: {ts!r}")
    txt = ts.strip()
    try:
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise TimeError(f"unparseable timestamp {ts!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def canonical(ts: str) -> str:
    """
    Normalise any readable timestamp to canonical form.

    This is what write paths call. It is idempotent, so passing an
    already-canonical value costs a regex match and changes nothing.
    """
    if isinstance(ts, str) and CANONICAL.match(ts):
        return ts
    return iso(parse(ts))


def is_canonical(ts: str) -> bool:
    return isinstance(ts, str) and bool(CANONICAL.match(ts))


def require_canonical(ts: str, field: str = "timestamp") -> str:
    """Assert canonical form, naming the field. For reads that must not silently coerce."""
    if not is_canonical(ts):
        raise TimeError(
            f"{field} {ts!r} is not canonical ({SQL_GLOB}). Comparisons in this "
            "project are lexicographic, so a differently-shaped timestamp sorts "
            "wrongly against every other one")
    return ts

"""
Signal collection: query-driven, anchored, and suspicious of its inputs.

`docs/EVIDENCE-REPORT.md` §3 established that lexical similarity finds
duplication and never events — two independent reports of one committee vote
shared two content words out of thirty-five. Independent reporting of an event
*is* lexically unrelated; that is what makes it independent. So collection here
is not a firehose that gets sorted afterwards: items are retrieved **for** a
registered proposition, the anchor is known at ingest, and which query produced
an item is recorded as provenance.

Two consequences that shape this module.

**Queries are predeclared, like reference classes.** A query written after
seeing which articles would have helped is a selection rule fitted to the
outcome, and it would poison the record in exactly the way §8 exists to prevent.
`declare_query()` stamps a time and the query is referenced by id thereafter.

**A failed run is recorded as a run.** A gap that looks like "no news that day"
is worse than a logged failure, because the first is indistinguishable from
evidence of quiet.

### Parsing hostile XML

Feeds are untrusted input from the open internet. `xml.etree.ElementTree` is
documented as vulnerable to entity-expansion attacks — a few hundred bytes of
nested entities expands to gigabytes and takes the process with it. This module
refuses any document containing a DOCTYPE or ENTITY declaration before parsing
begins, and caps the input size. Neither guard is clever; both are the kind that
has to exist before the first real feed is read rather than after.
"""

from __future__ import annotations

import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from . import evidence
from . import timeutil

# One timestamp format for the whole project. spine/timeutil.py has the
# lexicographic-ordering bug that made this non-negotiable; six copies of
# these helpers used to live in six modules and disagreed on whole seconds.
_now = timeutil.now
_iso = timeutil.iso
_parse = timeutil.parse
_canon = timeutil.canonical

USER_AGENT = "spine-collect/0.1"

# A feed that does not fit in this is not a feed we should be parsing.
MAX_FEED_BYTES = 8 * 1024 * 1024

# Entity expansion lives in the internal subset; no legitimate news feed needs
# either declaration.
_HOSTILE = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


class CollectError(RuntimeError):
    """A collection step would have recorded something misleading."""


class FeedUnreachable(CollectError):
    """The feed could not be fetched. Distinct from a feed that parsed badly."""


class HostileFeed(CollectError):
    """The document carries declarations no news feed needs."""






# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Entry:
    guid: str
    title: str
    body: str
    link: str | None = None
    claimed_published_at: str | None = None
    authors: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """Title and body together — what duplicate detection actually compares."""
        return f"{self.title}\n\n{self.body}".strip()


def _clean(text: str | None) -> str:
    """Strip tags and collapse whitespace. Feeds mix escaped and raw HTML freely."""
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", no_tags).strip()


def _when(raw: str | None) -> str | None:
    """RFC 822 (RSS) or ISO 8601 (Atom). Returns None rather than guessing."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        return _iso(parsedate_to_datetime(raw))
    except (TypeError, ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return _iso(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))
    except ValueError:
        return None


def parse_feed(data: bytes) -> list[Entry]:
    """
    RSS 2.0 or Atom to entries. Refuses hostile documents before parsing.

    An entry with no identifier *and* no link is dropped: without one there is
    nothing stable to deduplicate against across runs, and re-ingesting the same
    article every hour would inflate the apparent volume of coverage — which is
    an input to `n_eff_sources`.
    """
    if len(data) > MAX_FEED_BYTES:
        raise HostileFeed(
            f"feed is {len(data)} bytes, over the {MAX_FEED_BYTES} limit")
    if _HOSTILE.search(data):
        raise HostileFeed(
            "document contains a DOCTYPE or ENTITY declaration. No news feed "
            "needs either, and entity expansion is how a few hundred bytes of "
            "XML becomes gigabytes of memory")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise CollectError(f"feed is not well-formed XML: {e}") from e

    entries: list[Entry] = []

    for item in root.iter("item"):                       # RSS
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        body = (_clean(item.findtext("content:encoded", namespaces=_NS))
                or _clean(item.findtext("description")))
        entries.append(Entry(
            guid=guid, title=_clean(item.findtext("title")), body=body,
            link=link or None,
            claimed_published_at=_when(item.findtext("pubDate")
                                       or item.findtext("dc:date", namespaces=_NS)),
            authors=[a for a in (_clean(item.findtext("author")),) if a]))

    for item in root.iter(f"{{{_NS['atom']}}}entry"):    # Atom
        link_el = item.find("atom:link", namespaces=_NS)
        link = (link_el.get("href") if link_el is not None else "") or ""
        guid = (item.findtext("atom:id", namespaces=_NS) or link).strip()
        if not guid:
            continue
        body = (_clean(item.findtext("atom:content", namespaces=_NS))
                or _clean(item.findtext("atom:summary", namespaces=_NS)))
        entries.append(Entry(
            guid=guid, title=_clean(item.findtext("atom:title", namespaces=_NS)),
            body=body, link=link or None,
            claimed_published_at=_when(
                item.findtext("atom:published", namespaces=_NS)
                or item.findtext("atom:updated", namespaces=_NS)),
            authors=[_clean(a.findtext("atom:name", namespaces=_NS))
                     for a in item.findall("atom:author", namespaces=_NS)]))

    return entries


def fetch_feed(url: str, timeout: int = 30) -> bytes:
    """Fetch a feed, reading at most `MAX_FEED_BYTES + 1` so a hostile server cannot stream forever."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(MAX_FEED_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise CollectError(f"HTTP {e.code} from {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise FeedUnreachable(f"cannot reach {url}: {e.reason}") from e


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def declare_query(
    con: sqlite3.Connection,
    *,
    proposition_id: int,
    source_id: int,
    feed_url: str,
    query_text: str | None = None,
    note: str | None = None,
    declared_at: str | None = None,
) -> int:
    """
    Predeclare a collection query against a registered proposition.

    Stamped with a time for the same reason a reference class is frozen before
    use: a query written once you know which articles would have helped is a
    selection rule fitted to the outcome.
    """
    if not con.execute("SELECT 1 FROM propositions WHERE id=?",
                       (proposition_id,)).fetchone():
        raise CollectError(
            f"no proposition {proposition_id}: collection is anchored to a "
            "registered proposition, never to a free-text topic")
    ts = _canon(declared_at) if declared_at else _now()
    cur = con.execute(
        """INSERT OR IGNORE INTO collection_queries
           (proposition_id, source_id, feed_url, query_text, declared_at, note)
           VALUES (?,?,?,?,?,?)""",
        (proposition_id, source_id, feed_url, query_text, ts, note))
    if cur.lastrowid and cur.rowcount:
        con.commit()
        return cur.lastrowid
    row = con.execute(
        "SELECT id FROM collection_queries WHERE proposition_id=? AND source_id=? "
        "AND feed_url=? AND query_text IS ? AND declared_at=?",
        (proposition_id, source_id, feed_url, query_text, ts)).fetchone()
    con.commit()
    return row[0]


def active_queries(con: sqlite3.Connection, as_of: str | None = None) -> list[dict]:
    """Queries declared by `as_of` and not retired."""
    ts = _canon(as_of) if as_of else _now()
    cur = con.execute(
        "SELECT q.id, q.proposition_id, q.source_id, q.feed_url, q.query_text, "
        "       s.name AS source_name "
        "FROM collection_queries q JOIN sources s ON s.id = q.source_id "
        "WHERE q.declared_at <= ? AND (q.retired_at IS NULL OR q.retired_at > ?) "
        "ORDER BY q.id", (ts, ts))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# running a query
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunResult:
    run_id: int
    query_id: int
    outcome: str
    seen: int
    ingested: int
    duplicate: int
    item_ids: list[int] = field(default_factory=list)
    detail: str | None = None

    def summary(self) -> str:
        if self.outcome != "ok":
            return f"query {self.query_id}: {self.outcome} — {self.detail}"
        s = (f"query {self.query_id}: {self.seen} seen, {self.ingested} new, "
             f"{self.duplicate} already held")
        return s + (f" ({self.detail})" if self.detail else "")


def run_query(
    con: sqlite3.Connection,
    query: dict,
    *,
    verification_lag_seconds: float = 600.0,
    item_class: str = "reportage",
    fetcher=None,
    ran_at: str | None = None,
) -> RunResult:
    """
    Fetch one query's feed and ingest what it returns.

    Failures are recorded, never raised past this point: one dead feed must not
    stop a collection sweep, and a run that vanished is indistinguishable from a
    quiet news day.

    The default 600-second verification lag is **a policy, not a measurement**.
    It asserts that an item takes ten minutes to become usable by a decision. If
    verification is in fact instant for a given source — an exchange feed, a
    court docket — that source should say so explicitly rather than inherit this.
    """
    ts = _canon(ran_at) if ran_at else _now()
    get = fetcher or fetch_feed

    def record(outcome, seen=0, ingested=0, dup=0, detail=None):
        cur = con.execute(
            """INSERT INTO collection_runs
               (query_id, ran_at, entries_seen, entries_ingested,
                entries_duplicate, outcome, detail)
               VALUES (?,?,?,?,?,?,?)""",
            (query["id"], ts, seen, ingested, dup, outcome, detail))
        con.commit()
        return cur.lastrowid

    try:
        raw = get(query["feed_url"])
    except CollectError as e:
        rid = record("fetch_failed", detail=str(e))
        return RunResult(rid, query["id"], "fetch_failed", 0, 0, 0, detail=str(e))

    try:
        entries = parse_feed(raw)
    except CollectError as e:
        rid = record("parse_failed", detail=str(e))
        return RunResult(rid, query["id"], "parse_failed", 0, 0, 0, detail=str(e))

    ingested, duplicate, ids = 0, 0, []
    rejected: list[str] = []
    rid = record("ok", len(entries), 0, 0)
    for e in entries:
        # A feed that yields an entry this module cannot ingest -- no text, an
        # unreadable timestamp -- must not abort the sweep. The docstring says
        # failures are recorded rather than raised past this point, and that has
        # to hold for the ingest step too, not only the fetch and parse steps.
        try:
            res = evidence.ingest_item(
                con, query["source_id"], e.text,
                first_seen_at=ts, item_class=item_class,
                verification_lag_seconds=verification_lag_seconds,
                url=e.link, claimed_published_at=e.claimed_published_at,
                title=e.title or None, body_ref=e.guid, now=ts)
        except evidence.EvidenceError as exc:
            rejected.append(f"{e.guid}: {exc}")
            continue
        if res.note.startswith("already ingested"):
            duplicate += 1
        else:
            ingested += 1
        ids.append(res.item_id)
        con.execute(
            "INSERT OR IGNORE INTO item_provenance(item_id, query_id, run_id) "
            "VALUES (?,?,?)", (res.item_id, query["id"], rid))

    detail = (f"{len(rejected)} entr{'y' if len(rejected) == 1 else 'ies'} "
              f"rejected: " + "; ".join(rejected[:3])) if rejected else None
    con.execute(
        "UPDATE collection_runs SET entries_ingested=?, entries_duplicate=?, "
        "detail=? WHERE id=?", (ingested, duplicate, detail, rid))
    con.commit()
    return RunResult(rid, query["id"], "ok", len(entries), ingested, duplicate,
                     ids, detail)


def sweep(
    con: sqlite3.Connection,
    as_of: str | None = None,
    fetcher=None,
    on_result=None,
    **kw,
) -> list[RunResult]:
    """Run every active query once. Returns a result per query, failures included."""
    out = []
    for q in active_queries(con, as_of):
        r = run_query(con, q, fetcher=fetcher, ran_at=as_of, **kw)
        out.append(r)
        if on_result:
            on_result(q, r)
    return out


def anchored_items(con: sqlite3.Connection, proposition_id: int,
                   as_of: str) -> list[dict]:
    """
    Items collected for one proposition and usable at `as_of`.

    This is what feeds `evidence.cluster_items` — the anchor comes from the
    query that retrieved the item, which is why the provenance table exists.
    """
    cur = con.execute(
        """SELECT DISTINCT i.id, i.title, i.body_ref, i.available_for_decision_at,
                  i.source_id
           FROM signal_items i
           JOIN item_provenance p ON p.item_id = i.id
           JOIN collection_queries q ON q.id = p.query_id
           WHERE q.proposition_id = ? AND i.available_for_decision_at <= ?
           ORDER BY i.available_for_decision_at""",
        (proposition_id, as_of))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

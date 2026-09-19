#!/usr/bin/env python3
"""
Collection validation: feed parsing, hostile input, predeclared queries, and
the provenance that makes an anchor checkable.

Run: python3 tests/test_collect.py
Stdlib only. Nothing here touches the network.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import collect, evidence, ledger, registry  # noqa: E402
from spine.collect import CollectError, HostileFeed  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 19, 16, 0, 0, tzinfo=timezone.utc)


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def at(**kw):
    return (NOW + timedelta(**kw)).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
 <channel>
  <title>Committee Watch</title>
  <item>
   <title>Committee advances the measure</title>
   <link>https://example.test/a1</link>
   <guid>urn:cw:a1</guid>
   <pubDate>Thu, 17 Sep 2026 14:05:00 +0000</pubDate>
   <description>&lt;p&gt;The committee voted 12-5 to advance the measure.&lt;/p&gt;</description>
   <author>R. Okonjo</author>
  </item>
  <item>
   <title>Chair confirms floor schedule</title>
   <link>https://example.test/a2</link>
   <guid>urn:cw:a2</guid>
   <pubDate>Thu, 17 Sep 2026 18:30:00 +0000</pubDate>
   <description>A vote is expected before the recess.</description>
  </item>
  <item>
   <title>Untracked</title>
   <description>No identifier and no link.</description>
  </item>
 </channel>
</rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
 <title>Clerk Notices</title>
 <entry>
  <id>urn:clerk:n1</id>
  <title>Minutes published</title>
  <link href="https://clerk.test/n1"/>
  <published>2026-09-17T19:00:00Z</published>
  <summary>Minutes for the sitting of 17 September are now available.</summary>
  <author><name>Committee Clerk</name></author>
 </entry>
</feed>"""

BOMB = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [ <!ENTITY lol "lol"> <!ENTITY lol2 "&lol;&lol;&lol;&lol;"> ]>
<rss version="2.0"><channel><item><title>&lol2;</title></item></channel></rss>"""


def main() -> int:
    print("=" * 76)
    print("SPINE COLLECTION VALIDATION")
    print("=" * 76)

    print("\n[1] Feeds are untrusted input\n")
    t0 = time.monotonic()
    ok("a document with a DOCTYPE is refused before parsing",
       raises(lambda: collect.parse_feed(BOMB), HostileFeed))
    ok("...quickly, because nothing was expanded", time.monotonic() - t0 < 0.5)
    ok("an oversized document is refused",
       raises(lambda: collect.parse_feed(b"<rss/>" + b" " * (9 * 1024 * 1024)),
              HostileFeed))
    ok("malformed XML is a parse failure, not a crash",
       raises(lambda: collect.parse_feed(b"<rss><channel>"), CollectError))
    ok("an ENTITY declaration without a DOCTYPE is also refused",
       raises(lambda: collect.parse_feed(b'<!ENTITY x "y"><rss/>'), HostileFeed))

    print("\n[2] RSS and Atom, without guessing\n")
    rss = collect.parse_feed(RSS)
    ok("two identifiable RSS items parse", len(rss) == 2, len(rss))
    ok("an item with neither guid nor link is dropped",
       all(e.guid for e in rss))
    ok("escaped HTML is stripped from the body",
       rss[0].body == "The committee voted 12-5 to advance the measure.",
       rss[0].body)
    ok("RFC 822 dates parse to ISO",
       rss[0].claimed_published_at == "2026-09-17T14:05:00.000Z",
       rss[0].claimed_published_at)
    ok("the author is captured when present", rss[0].authors == ["R. Okonjo"])
    ok("a missing author is an empty list, not a placeholder", rss[1].authors == [])

    atom = collect.parse_feed(ATOM)
    ok("Atom entries parse", len(atom) == 1 and atom[0].guid == "urn:clerk:n1")
    ok("ISO dates parse", atom[0].claimed_published_at == "2026-09-17T19:00:00.000Z")
    ok("the link comes from the href attribute",
       atom[0].link == "https://clerk.test/n1")
    ok("an unparseable date is None rather than a guess",
       collect.parse_feed(
           b'<rss><channel><item><guid>g</guid><pubDate>whenever</pubDate>'
           b'</item></channel></rss>')[0].claimed_published_at is None)
    ok("title and body together are what duplicate detection compares",
       rss[0].title in rss[0].text and rss[0].body in rss[0].text)

    # ---------------------------------------------------- queries
    print("\n[3] Queries are predeclared and anchored to a proposition\n")
    con = ledger.connect(":memory:", create=True)
    src_cw = evidence.ensure_source(con, "Committee Watch", "outlet", now=at())
    src_clerk = evidence.ensure_source(con, "Committee Clerk", "official", now=at())
    pid = registry.ensure_proposition(
        con, "The measure reaches a floor vote",
        "A recorded floor vote before the deadline",
        at(days=20), "committee_adoption", "T1", now=at())

    q1 = collect.declare_query(con, proposition_id=pid, source_id=src_cw,
                               feed_url="https://cw.test/rss", query_text="measure",
                               declared_at=at())
    q2 = collect.declare_query(con, proposition_id=pid, source_id=src_clerk,
                               feed_url="https://clerk.test/atom", declared_at=at())
    ok("a query registers against a proposition", q1 > 0 and q2 > 0)
    ok("declaring the same query twice is idempotent",
       collect.declare_query(con, proposition_id=pid, source_id=src_cw,
                             feed_url="https://cw.test/rss", query_text="measure",
                             declared_at=at()) == q1)
    ok("a query against a proposition that does not exist is refused",
       raises(lambda: collect.declare_query(
           con, proposition_id=9999, source_id=src_cw, feed_url="x"), CollectError))
    ok("both queries are active", len(collect.active_queries(con, at(hours=1))) == 2)
    ok("a query is not active before it was declared",
       collect.active_queries(con, at(hours=-1)) == [])
    con.execute("UPDATE collection_queries SET retired_at=? WHERE id=?",
                (at(hours=2), q2))
    con.commit()
    ok("a retired query drops out of the sweep",
       len(collect.active_queries(con, at(hours=3))) == 1)
    con.execute("UPDATE collection_queries SET retired_at=NULL WHERE id=?", (q2,))
    con.commit()

    # ---------------------------------------------------- running
    print("\n[4] A sweep, including the feed that is down\n")
    feeds = {"https://cw.test/rss": RSS, "https://clerk.test/atom": ATOM}

    def fetch(url):
        if url not in feeds:
            raise collect.FeedUnreachable(f"cannot reach {url}")
        return feeds[url]

    results = collect.sweep(con, as_of=at(hours=1), fetcher=fetch,
                            verification_lag_seconds=600)
    ok("every active query produced a result", len(results) == 2)
    ok("the RSS query ingested two items",
       results[0].ingested == 2 and results[0].seen == 2, results[0].summary())
    ok("the Atom query ingested one", results[1].ingested == 1)
    for r in results:
        print(f"        {r.summary()}")

    again = collect.sweep(con, as_of=at(hours=2), fetcher=fetch)
    ok("re-running recognises everything as already held",
       all(r.ingested == 0 for r in again) and sum(r.duplicate for r in again) == 3,
       [r.summary() for r in again])

    feeds.pop("https://cw.test/rss")
    down = collect.sweep(con, as_of=at(hours=3), fetcher=fetch)
    ok("a dead feed does not stop the sweep",
       len(down) == 2 and down[1].outcome == "ok", [r.outcome for r in down])
    ok("...and the failure is recorded as a run, not as silence",
       down[0].outcome == "fetch_failed", down[0].summary())
    ok("a failed run is in the database with its reason",
       con.execute("SELECT COUNT(*) FROM collection_runs WHERE outcome='fetch_failed' "
                   "AND detail IS NOT NULL").fetchone()[0] == 1)

    feeds["https://cw.test/rss"] = b"<rss><channel>"
    bad = collect.sweep(con, as_of=at(hours=4), fetcher=fetch)
    ok("a malformed feed is a parse failure, distinct from a fetch failure",
       bad[0].outcome == "parse_failed", bad[0].summary())
    ok("the two failure kinds are distinguishable in the record",
       {r[0] for r in con.execute(
           "SELECT DISTINCT outcome FROM collection_runs")} ==
       {"ok", "fetch_failed", "parse_failed"})

    # ---------------------------------------------------- provenance
    print("\n[5] Provenance makes the anchor checkable\n")
    items = collect.anchored_items(con, pid, at(hours=5))
    ok("three items are anchored to the proposition", len(items) == 3, len(items))
    ok("an item appears once however many runs saw it",
       len({i["id"] for i in items}) == 3)
    ok("provenance rows outnumber items, because runs repeat",
       con.execute("SELECT COUNT(*) FROM item_provenance").fetchone()[0] > 3)
    ok("every item traces back to the query that retrieved it",
       con.execute(
           "SELECT COUNT(*) FROM signal_items i WHERE NOT EXISTS "
           "(SELECT 1 FROM item_provenance p WHERE p.item_id=i.id)").fetchone()[0]
       == 0)
    ok("an item is not visible before its verification lag elapses",
       collect.anchored_items(con, pid, at(hours=1, minutes=5)) == [])
    ok("...and is visible after", len(collect.anchored_items(con, pid, at(hours=1, minutes=11))) == 3)
    ok("a different proposition sees none of them",
       collect.anchored_items(con, registry.ensure_proposition(
           con, "Something else", "c", at(days=30), "other", "T2", now=at()),
           at(hours=5)) == [])

    # The join the whole architecture rests on.
    print("\n[6] Collected items cluster under their anchor\n")
    rows = [{"id": i["id"], "body": i["title"] or "",
             "anchor": f"prop:{pid}",
             "available_for_decision_at": i["available_for_decision_at"]}
            for i in items]
    clusters = evidence.cluster_items(con, rows, cluster_version=1)
    ok("items retrieved for one proposition form one cluster",
       len(clusters) == 1 and len(clusters[0].item_ids) == 3,
       [c.item_ids for c in clusters])
    ok("...even though their headlines share almost no vocabulary",
       max(evidence.overlap(evidence.topic_tokens(a["body"]),
                            evidence.topic_tokens(b["body"]))
           for a in rows for b in rows if a["id"] != b["id"]) < 0.35)
    eff = evidence.n_eff_sources(con, [i["source_id"] for i in items],
                                 at(hours=5))
    ok("two distinct sources across three items are worth fewer than two",
       eff.n_sources == 2 and eff.n_eff < 2.0, eff.summary())
    print(f"        {eff.summary()}")

    ok("the schema refuses a run claiming more ingested than seen",
       raises(lambda: con.execute(
           "INSERT INTO collection_runs(query_id, ran_at, entries_seen, "
           "entries_ingested, entries_duplicate, outcome) VALUES(?,?,1,5,0,'ok')",
           (q1, at())), sqlite3.IntegrityError))

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

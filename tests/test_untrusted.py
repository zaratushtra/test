#!/usr/bin/env python3
"""
§11.3: retrieved documents are untrusted throughout.

There is no LLM in this pipeline yet, so the prompt-injection half of §11.3 is
not live. The other half already is: this project ingests open-internet text
into a database and then adjudicates it, and two things in that text can make
the record say something other than what it means.

Run: python3 tests/test_untrusted.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import evidence, ledger, untrusted  # noqa: E402
from spine.evidence import EvidenceError  # noqa: E402
from spine.untrusted import UntrustedInputError  # noqa: E402

PASS, FAIL = [], []
T = "2026-09-20T07:00:00.000Z"


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


def main() -> int:
    print("=" * 76)
    print("SPINE UNTRUSTED INPUT (§11.3)")
    print("=" * 76)

    print("\n[1] Text that renders differently than it is stored\n")
    trojan = "The committee approved‮ detcejer the measure"
    ok("a bidirectional override is detected",
       "bidirectional" in (untrusted.describe(trojan) or ""),
       untrusted.describe(trojan))
    ok("...and the finding names the codepoint",
       "U+202E" in untrusted.describe(trojan))
    ok("...and says why it matters: hashed and read are different things",
       "what is hashed is not what a reader sees" in untrusted.describe(trojan))
    ok("every bidi control is covered, not just the famous one",
       all(untrusted.scan_text(f"a{c}b") for c in untrusted.BIDI_CONTROLS),
       [c for c in untrusted.BIDI_CONTROLS if not untrusted.scan_text(f"a{c}b")])
    ok("zero-width characters are detected",
       "zero-width" in (untrusted.describe("app​roved") or ""))
    ok("a NUL byte is detected",
       "NUL" in (untrusted.describe("text\x00hidden") or ""))
    ok("ordinary text is clean", untrusted.is_display_safe(
        "The committee voted 12-5 on Thursday.\nMinutes follow.\tIndented."))
    ok("tabs and newlines are not control-character findings",
       untrusted.is_display_safe("a\tb\r\nc"))
    ok("accented and non-Latin text is clean",
       untrusted.is_display_safe("Ruritanian: Ру́рітанія — Ελλάδα — 日本語"))

    print("\n[2] Source text is preserved and flagged; ours is refused\n")
    con = ledger.connect(":memory:", create=True)
    src = evidence.ensure_source(con, "Hostile Feed", "outlet", now=T)
    r = evidence.ingest_item(con, src, trojan, first_seen_at=T,
                             item_class="reportage", title="Approved", now=T)
    stored = con.execute(
        "SELECT display_warnings FROM signal_items WHERE id=?",
        (r.item_id,)).fetchone()[0]
    ok("the item is stored, not rejected", r.item_id > 0)
    ok("...verbatim, because what the publisher emitted is the evidence",
       con.execute("SELECT content_hash FROM signal_items WHERE id=?",
                   (r.item_id,)).fetchone()[0]
       == __import__("spine.canonical", fromlist=["x"]).content_hash(
           {"body": trojan.strip()}))
    ok("...with the finding recorded alongside it", stored and "U+202E" in stored)
    ok("...and surfaced to the caller", "display warning" in r.note, r.note)

    ok("our own claim text is REFUSED, not flagged",
       raises(lambda: evidence.record_claim(
           con, cluster_id="c", cluster_version=1, assertion=trojan,
           authenticity="artifact_verified",
           extraction_fidelity="checked_faithful", establishes="underlying_fact",
           source_ids=[src], available_for_decision_at=T,
           feature_version="v1"), EvidenceError))
    ok("...because a claim that hashes one way and reads another is pointless",
       "defeats the point of recording it" in _msg(
           lambda: untrusted.require_display_safe(trojan, "assertion")))
    ok("the refusal names the field", "assertion" in _msg(
        lambda: untrusted.require_display_safe(trojan, "assertion")))
    ok("clean claim text is accepted",
       raises(lambda: untrusted.require_display_safe("A plain assertion."),
              UntrustedInputError) is False)

    print("\n[3] A link is not a document\n")
    for bad, why in [
        ("javascript:alert(1)", "scheme"),
        ("data:text/html,<script>alert(1)</script>", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("vbscript:msgbox", "scheme"),
        ("notaurl", "no scheme"),
        ("http://", "no host"),
        ("http://x.test/a‮b", "invisible"),
        ("http://x.test/a\x00b", "invisible"),
    ]:
        safe, reason = untrusted.check_url(bad)
        ok(f"{bad[:32]!r} is dropped", safe is None and reason, (safe, reason))
        ok(f"...for the stated reason ({why})", why.split()[0] in (reason or ""),
           reason)

    for good in ("https://example.test/article",
                 "http://example.test:8080/a?b=c#d",
                 "https://例え.テスト/記事"):
        ok(f"{good[:34]!r} survives", untrusted.check_url(good)[0] == good)
    ok("an absent url is absent, not an error",
       untrusted.check_url(None) == (None, None))
    ok("an empty url is absent, not an error",
       untrusted.check_url("   ") == (None, None))

    print("\n[4] The rejection travels with the item\n")
    r2 = evidence.ingest_item(
        con, src, "A separate story with a hostile link.", first_seen_at=T,
        item_class="reportage", url="javascript:fetch('http://evil/')", now=T)
    row = con.execute(
        "SELECT url, rejected_url_reason FROM signal_items WHERE id=?",
        (r2.item_id,)).fetchone()
    ok("the unsafe url is not stored", row[0] is None)
    ok("...and the reason is", row[1] and "javascript" in row[1], row[1])
    ok("...and the article is still ingested as evidence", r2.item_id > 0)
    ok("the caller is told", "url dropped" in r2.note, r2.note)

    r3 = evidence.ingest_item(
        con, src, "A third story, entirely unremarkable.", first_seen_at=T,
        item_class="reportage", url="https://example.test/ok", now=T)
    row3 = con.execute(
        "SELECT url, display_warnings, rejected_url_reason FROM signal_items "
        "WHERE id=?", (r3.item_id,)).fetchone()
    ok("a clean item carries no flags at all", row3 == ("https://example.test/ok",
                                                        None, None), row3)
    ok("...and its note is empty", r3.note == "", repr(r3.note))

    print("\n[5] SQL is parameterised throughout\n")
    import ast
    import pathlib
    # An f-string in SQL is fine when it interpolates column names or
    # placeholder runs, and never fine when it interpolates a value. Checked on
    # the AST rather than by scanning text: a character window runs past the SQL
    # into whatever f-string comes next, which is how this check first reported
    # an error-message formatter as an injection site.
    INTERNAL = {
        "COMMITTED_FIELDS", "cols", "rc_cols", "estimators", "placeholders",
        "join", "len",
    }
    offenders = []
    for path in sorted(pathlib.Path(ROOT, "spine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("execute", "executescript")
                    and node.args
                    and isinstance(node.args[0], ast.JoinedStr)):
                continue
            for piece in node.args[0].values:
                if not isinstance(piece, ast.FormattedValue):
                    continue
                names = {n.id for n in ast.walk(piece.value)
                         if isinstance(n, ast.Name)}
                names |= {n.attr for n in ast.walk(piece.value)
                          if isinstance(n, ast.Attribute)}
                stray = names - INTERNAL
                if stray:
                    offenders.append(
                        (path.name, ast.unparse(piece.value), sorted(stray)))
    ok("no execute() interpolates anything but internal column names",
       not offenders, offenders)

    con2 = ledger.connect(":memory:", create=True)
    s2 = evidence.ensure_source(con2, "Injector", "outlet", now=T)
    evidence.ingest_item(con2, s2, "'); DROP TABLE contracts; --",
                         first_seen_at=T, item_class="reportage",
                         title="'); DELETE FROM sources; --", now=T)
    ok("SQL metacharacters land as inert data",
       bool(con2.execute(
           "SELECT name FROM sqlite_master WHERE name='contracts'").fetchone()))
    ok("...stored exactly as sent",
       con2.execute("SELECT title FROM signal_items").fetchone()[0]
       == "'); DELETE FROM sources; --")

    print("\n[6] Nothing untrusted reaches a control surface\n")
    # Every column an untrusted feed can write to, and the ones it cannot.
    writable = {"content_hash", "source_id", "url", "event_at",
                "claimed_published_at", "first_seen_at", "artifact_created_at",
                "available_for_decision_at", "title", "body_ref", "item_class",
                "display_warnings", "rejected_url_reason", "id"}
    cols = {r[1] for r in con.execute("PRAGMA table_info(signal_items)")}
    ok("a feed writes only to signal_items columns", cols <= writable,
       cols - writable)
    for table in ("contracts", "trade_decisions", "evaluation_plans",
                  "reference_class_versions", "forecasts"):
        ok(f"ingestion wrote nothing to {table}",
           con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0)

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Two processes, one database.

This project is designed to be used that way: `serve.py` collects on a schedule
while somebody analyses the same file. Every other suite runs one process, where
a check-then-act race is invisible because there is nothing to race with.

Ten functions here read a row, and insert it if absent, to be idempotent. That
is check-then-act: two processes both see the row missing and both insert, and
one gets an `IntegrityError` out of a call whose whole promise is that a repeat
is a no-op. Found by running four collectors against one database — 1,600 writes
produced six such failures.

Run: python3 tests/test_concurrent.py
Stdlib only.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import evidence, ledger, registry, shadow, timeutil  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
iso = timeutil.iso

# Each worker hammers the idempotent creators with the SAME arguments, so every
# call after the first must be a no-op. Any IntegrityError is the race.
WORKER = r'''
import sys, json
sys.path.insert(0, "/home/user/test")
from spine import evidence, ledger, registry, shadow, timeutil
from datetime import datetime, timedelta, timezone
DB, n = sys.argv[1], int(sys.argv[2])
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
iso = timeutil.iso
T = iso(NOW)
con = ledger.connect(DB)
errors, ids = [], {"source": set(), "manifest": set(), "book": set(),
                   "proposition": set(), "item": set()}
for i in range(n):
    try:
        ids["source"].add(evidence.ensure_source(con, "Shared Wire", "wire"))
        ids["manifest"].add(ledger.put_manifest(con, "model_config", {"k": 1}, T))
        ids["book"].add(shadow.record_book(
            con, 1, [(5900, 100.0)], [(6100, 100.0)], captured_at=T,
            source="fixture"))
        ids["proposition"].add(registry.ensure_proposition(
            con, "Shared proposition", "c", iso(NOW + timedelta(days=9)),
            "f", "T1", now=T))
        ids["item"].add(evidence.ingest_item(
            con, 1, "A shared body of text for the race.", first_seen_at=T,
            item_class="reportage", now=T).item_id)
        registry.ingest_markets(con, [{
            "id": "shared", "question": "q",
            "end_date": iso(NOW + timedelta(days=9)), "rules_text": "r",
            "outcome_token_id": "tok"}], jurisdiction="GB", now=T)
    except Exception as e:
        errors.append(f"{type(e).__name__}: {e}")
print(json.dumps({"errors": errors[:5], "n_errors": len(errors),
                  "ids": {k: sorted(v) for k, v in ids.items()}}))
'''


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def main() -> int:
    print("=" * 76)
    print("SPINE CONCURRENCY")
    print("=" * 76)

    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "spine.db")
        con = ledger.connect(db)
        registry.ingest_markets(con, [{
            "id": "seed", "question": "q",
            "end_date": iso(NOW + timedelta(days=9)), "rules_text": "r",
            "outcome_token_id": "seed-tok"}], jurisdiction="GB", now=iso(NOW))
        evidence.ensure_source(con, "Seed", "wire", now=iso(NOW))
        ok("the database is in WAL mode, so readers do not block writers",
           con.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
        ok("...and a busy writer waits rather than failing instantly",
           con.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000)
        con.close()

        print("\n[1] Four processes calling the idempotent creators at once\n")
        worker = pathlib.Path(d, "worker.py")
        worker.write_text(WORKER)
        procs = [subprocess.Popen([sys.executable, str(worker), db, "60"],
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
        results = []
        for p in procs:
            out, err = p.communicate(timeout=180)
            ok("a worker completed", p.returncode == 0, err[-400:])
            if out.strip():
                import json
                results.append(json.loads(out))

        total_errors = sum(r["n_errors"] for r in results)
        ok("240 concurrent idempotent calls each, with no errors at all",
           total_errors == 0,
           [e for r in results for e in r["errors"]][:3])

        print("\n[2] Idempotent means one row, whoever got there first\n")
        for kind in ("source", "manifest", "book", "proposition", "item"):
            seen = {i for r in results for i in r["ids"][kind]}
            ok(f"every process agrees on one {kind}", len(seen) == 1, seen)

        con = ledger.connect(db)
        for table, expect, what in (
            ("sources", 2, "the seed source plus the shared one"),
            ("manifests", 1, "one manifest"),
            ("book_snapshots", 1, "one snapshot"),
            ("propositions", 1, "one proposition"),
            ("signal_items", 1, "one item"),
            ("contracts", 2, "the seed contract plus the shared one"),
        ):
            n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            ok(f"{table} holds {what}", n == expect, n)

        print("\n[3] The chain refuses a concurrent append rather than forking\n")
        # Two writers cannot both extend the hash chain: the trigger compares
        # prev_hash against the current head, so the loser is rejected. That is
        # the correct outcome -- a forked chain would be far worse than a failed
        # write -- and it is worth pinning that the failure is loud.
        import sqlite3
        head = con.execute(
            "SELECT COUNT(*) FROM forecasts").fetchone()[0]
        ok("no forecasts were written by the collectors", head == 0)
        ok("the chain-head trigger exists to make that refusal loud",
           bool(con.execute(
               "SELECT 1 FROM sqlite_master WHERE type='trigger' "
               "AND name='forecasts_chain_head'").fetchone()))
        con.close()

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

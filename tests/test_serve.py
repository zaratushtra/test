#!/usr/bin/env python3
"""
The scheduled collector: signals, failure handling, and restart safety.

A process that runs for weeks fails in ways a function does not. This suite
covers the four that matter: a stop signal mid-run, a pass that fails, a pass
that hangs, and a restart that must not duplicate what the last run wrote.

Run: python3 tests/test_serve.py
Stdlib only. Nothing here touches the network.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS, FAIL = [], []


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def fixtures(d: str) -> str:
    """A saved venue response the cycle can replay offline."""
    saved = os.path.join(d, "saved")
    os.makedirs(saved, exist_ok=True)
    with open(os.path.join(saved, "markets.json"), "w", encoding="utf-8") as fh:
        json.dump([{"id": f"s{i}", "question": f"Will {i} happen?",
                    "endDate": "2026-09-28T00:00:00Z", "liquidityNum": 40000,
                    "description": "Resolves on the named source.",
                    "clobTokenIds": json.dumps([f"st{i}", f"st{i}b"]),
                    "events": [{"id": f"e{i}", "tags": [{"label": "Policy"}]}]}
                   for i in range(3)], fh)
    with open(os.path.join(saved, "books.json"), "w", encoding="utf-8") as fh:
        json.dump({f"st{i}": {"bids": [{"price": "0.60", "size": "100"}],
                              "asks": [{"price": "0.62", "size": "100"}]}
                   for i in range(3)}, fh)
    return saved


def serve(*args, **kw):
    return subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "serve.py"), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT, **kw)


def main() -> int:
    print("=" * 76)
    print("SPINE SCHEDULED COLLECTOR")
    print("=" * 76)

    with tempfile.TemporaryDirectory() as d:
        saved = fixtures(d)
        db = os.path.join(d, "spine.db")
        hb = os.path.join(d, "health.json")
        cycle = ["--", "--from-dir", saved, "--db", db, "--jurisdiction", "GB"]

        print("\n[1] A bounded run, and what it wrote\n")
        p = serve("--interval", "0.1", "--max-passes", "2", "--heartbeat", hb,
                  "--quiet", *cycle)
        out, err = p.communicate(timeout=120)
        ok("a bounded run exits zero", p.returncode == 0, err[-400:])
        ok("...after the requested number of passes",
           "reached 2 passes" in out, out[-300:])
        h = json.load(open(hb, encoding="utf-8"))
        ok("the heartbeat records the final state",
           h["state"] == "stopped" and h["passes"] == 2, h)
        ok("...and that nothing was failing", h["consecutive_failures"] == 0, h)

        import sqlite3
        con = sqlite3.connect(db)
        contracts = con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0]
        books = con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0]
        ok("two passes registered each contract once", contracts == 3, contracts)
        ok("...and recorded a book each pass, because a later capture is new data",
           books == 6, books)
        con.close()

        print("\n[2] Restarting does not duplicate what the last run wrote\n")
        p2 = serve("--interval", "0.1", "--max-passes", "1", "--heartbeat", hb,
                   "--quiet", *cycle)
        p2.communicate(timeout=120)
        con = sqlite3.connect(db)
        ok("a restart registers no new contracts",
           con.execute("SELECT COUNT(*) FROM contracts").fetchone()[0] == 3)
        ok("...and adds exactly one book per contract",
           con.execute("SELECT COUNT(*) FROM book_snapshots").fetchone()[0] == 9)
        con.close()

        print("\n[3] SIGTERM finishes the pass and exits cleanly\n")
        # A fresh heartbeat path: reusing one would let the readiness loop below
        # read the PREVIOUS run's "passes: 2" and signal before this process had
        # installed its handler -- which is how this check first passed for the
        # wrong reason and then failed for the right one.
        hb_sig = os.path.join(d, "health-sigterm.json")
        p3 = serve("--interval", "30", "--heartbeat", hb_sig, "--quiet", *cycle)
        # Let the first pass start and the loop settle into its sleep.
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if json.load(open(hb_sig, encoding="utf-8")).get("passes", 0) >= 1:
                    break
            except (json.JSONDecodeError, FileNotFoundError):
                pass
            time.sleep(0.2)
        p3.send_signal(signal.SIGTERM)
        t0 = time.time()
        out3, err3 = p3.communicate(timeout=60)
        took = time.time() - t0
        ok("SIGTERM is handled, not fatal", p3.returncode == 0, p3.returncode)
        ok("...and the process says what it is doing",
           "SIGTERM received" in out3, out3[-300:])
        ok("...and stops cleanly rather than being killed",
           "stopped cleanly" in out3, out3[-300:])
        ok("...promptly, rather than after the full 30s interval",
           took < 20, f"{took:.1f}s")
        h3 = json.load(open(hb_sig, encoding="utf-8"))
        ok("the heartbeat records the stop", h3["state"] == "stopped", h3)
        print(f"        exited {took:.1f}s after the signal, interval was 30s")

        print("\n[4] A failing pass does not stop the loop, forever\n")
        bad = ["--", "--from-dir", os.path.join(d, "nonexistent"),
               "--db", db, "--jurisdiction", "GB"]
        p4 = serve("--interval", "0.05", "--max-consecutive-failures", "3",
                   "--heartbeat", hb, *bad)
        out4, err4 = p4.communicate(timeout=120)
        ok("repeated failure exits non-zero", p4.returncode == 1, p4.returncode)
        ok("...after the configured number of consecutive failures",
           "3 consecutive failures" in out4, out4[-400:])
        ok("...saying why stopping beats failing silently",
           "fails silently forever" in out4)
        h4 = json.load(open(hb, encoding="utf-8"))
        ok("the heartbeat reports failing, not merely alive",
           h4["state"] == "failing" and h4["consecutive_failures"] == 3, h4)
        ok("...and carries the exit code of the failing pass",
           h4["last_exit_code"] != 0, h4)

        print("\n[5] A hung pass is killed rather than waited on\n")
        p5 = serve("--interval", "0.05", "--max-passes", "1", "--pass-timeout",
                   "1", "--heartbeat", hb, "--", "--from-dir", saved,
                   "--db", db, "--jurisdiction", "GB")
        out5, err5 = p5.communicate(timeout=120)
        ok("a pass under the timeout still succeeds", p5.returncode == 0,
           err5[-300:])

        hb2 = os.path.join(d, "health2.json")
        p6 = serve("--interval", "0.05", "--max-passes", "1", "--pass-timeout",
                   "0.001", "--max-consecutive-failures", "1",
                   "--heartbeat", hb2, *cycle)
        out6, err6 = p6.communicate(timeout=120)
        ok("a pass exceeding the timeout is killed and counted as a failure",
           p6.returncode == 1 and "exit 124" in out6, out6[-300:])
        ok("...and the reason reaches the log",
           "exceeded" in err6 or "exceeded" in out6)

        print("\n[5b] A bounded run whose last pass failed does not report success\n")
        hb3 = os.path.join(d, "health3.json")
        p7 = serve("--interval", "0.05", "--max-passes", "2",
                   "--max-consecutive-failures", "99", "--heartbeat", hb3, *bad)
        out7, _ = p7.communicate(timeout=120)
        ok("all passes failed but the pass budget was reached",
           "reached 2 passes" in out7, out7[-300:])
        ok("...and the exit code is still non-zero", p7.returncode == 1,
           p7.returncode)
        ok("...which is what stops a fully broken run looking like a working one",
           json.load(open(hb3, encoding="utf-8"))["last_exit_code"] != 0)

        print("\n[6] The heartbeat is written atomically\n")
        ok("no temporary file is left behind",
           not os.path.exists(hb + ".tmp"))
        ok("the heartbeat is valid JSON every time it is read",
           isinstance(json.load(open(hb, encoding="utf-8")), dict))

        print("\n[6b] The heartbeat never kills the loop\n")
        nested = os.path.join(d, "does", "not", "exist", "health.json")
        p8 = serve("--interval", "0.05", "--max-passes", "1", "--quiet",
                   "--heartbeat", nested, *cycle)
        out8, err8 = p8.communicate(timeout=120)
        ok("a heartbeat path in a missing directory does not crash the process",
           p8.returncode == 0, err8[-400:])
        ok("...the directory is created instead", os.path.exists(nested))

        # A path whose parent is a FILE, not a directory. Unlike chmod, this
        # fails for root too, so the check does not quietly pass depending on
        # who the test runs as -- which is how it first appeared to pass.
        blocker = os.path.join(d, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        p9 = serve("--interval", "0.05", "--max-passes", "1", "--quiet",
                   "--heartbeat", os.path.join(blocker, "h.json"), *cycle)
        out9, err9 = p9.communicate(timeout=120)
        ok("an unwritable heartbeat is reported, not fatal",
           p9.returncode == 0 and "could not write heartbeat" in err9,
           (p9.returncode, err9[-300:]))
        ok("...and the pass still ran", "pass 1" in out9 or "reached 1" in out9,
           out9[-200:])

        print("\n[7] There is no secret handling, by construction\n")
        src = open(os.path.join(ROOT, "serve.py"), encoding="utf-8").read()
        dockerfile = open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8").read()
        for token in ("secret", "SECRET", "token", "password", "credential"):
            ok(f"serve.py mentions no {token!r} mechanism",
               token not in src.replace("secrets mount would be", "")
               .replace("no secret handling", ""),
               [l for l in src.splitlines() if token in l][:1])
        ok("the Dockerfile mounts no secrets",
           "/run/secrets" not in dockerfile)
        ok("the Dockerfile runs as a non-root user", "USER spine" in dockerfile)
        ok("...and the code is not writable by that user",
           "chmod -R a-w /app" in dockerfile)
        ok("tini is the entrypoint, so SIGTERM reaches the process",
           'ENTRYPOINT ["/usr/bin/tini"' in dockerfile)
        ok("the healthcheck reads the heartbeat, not liveness",
           "health.json" in dockerfile and "failing" in dockerfile)
        # The prose in the Dockerfile says there is no pip install, so match the
        # instruction rather than the word.
        ok("nothing is pip installed: there is no dependency surface",
           not re.search(r"^\s*RUN[^\n]*pip\s+install", dockerfile, re.MULTILINE))
        ok("...and there is no requirements file to drift",
           not os.path.exists(os.path.join(ROOT, "requirements.txt")))

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

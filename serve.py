#!/usr/bin/env python3
"""
The long-running collector: run the cycle on an interval, die cleanly.

`run_cycle.py` is one pass. Shadow execution needs a *history* of books, and the
evidence pipeline needs feeds swept repeatedly, so something has to keep running.
This is that thing, and it is deliberately small.

What it does about the things that actually go wrong when a process runs for
weeks:

**SIGTERM arrives mid-cycle.** A container stop, a deploy, an OOM kill. The
handler sets a flag; the current pass finishes and the loop exits. It does not
abort a pass halfway, because a pass writes several independent rows and the
half-written state is harder to reason about than either end.

**A pass fails.** Network blips, the venue changes a field, a feed returns HTML.
One bad pass must not stop the loop — but a loop that fails silently forever is
worse than one that stops, so consecutive failures are counted and the process
exits non-zero once they pass a threshold. A supervisor restarting it is the
right response; pretending to work is not.

**Restart duplicates data.** It does not: contracts are keyed by rules-version
hash, items by content hash, manifests by content. A restart re-registers
nothing. Book snapshots *are* re-recorded, because a later capture is a new
observation — that is the point of running on an interval.

**Nobody is watching.** A heartbeat file is written after each pass with the
outcome, so a health check reads state rather than inferring it from the process
being alive. A process that is alive and failing every pass is the case worth
catching.

There is no secret handling here, and no mechanism to add one. Under §2.1's
paper-only posture the venue endpoints are public and the client has no
authentication path at all, so a secrets mount would be a facility with no use
and a liability with one.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))

_stop = False


def _handle(signum, _frame):
    global _stop
    _stop = True
    print(f"\n[serve] {signal.Signals(signum).name} received; finishing the "
          "current pass and exiting", flush=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def write_heartbeat(path: str, **fields) -> None:
    """
    Atomic: write beside, then rename. A half-written heartbeat reads as healthy.

    Never raises. The heartbeat is observability, not the work — a process that
    died because it could not report its health would be reporting its health by
    dying, which is the least useful moment to fail. A write problem is printed
    once per pass and the loop continues.
    """
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"at": now(), **fields}, fh, indent=2)
        os.replace(tmp, path)
    except OSError as e:
        print(f"[serve] could not write heartbeat to {path}: {e}",
              file=sys.stderr, flush=True)


def run_pass(args: list[str], timeout: float) -> tuple[int, str]:
    """One cycle, as a subprocess so a crash in it cannot take the loop down."""
    try:
        p = subprocess.run([sys.executable, os.path.join(ROOT, "run_cycle.py"), *args],
                           capture_output=True, text=True, cwd=ROOT, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"pass exceeded {timeout}s and was killed"


def main() -> int:
    ap = argparse.ArgumentParser(description="SPINE scheduled collector")
    ap.add_argument("--interval", type=float, default=3600.0,
                    help="seconds between passes (default 3600)")
    ap.add_argument("--max-passes", type=int, default=0,
                    help="stop after N passes; 0 means run until signalled")
    ap.add_argument("--max-consecutive-failures", type=int, default=5,
                    help="exit non-zero after this many failures in a row")
    ap.add_argument("--pass-timeout", type=float, default=1800.0,
                    help="kill a pass that exceeds this many seconds")
    ap.add_argument("--heartbeat", default="",
                    help="path to write pass status to after each pass")
    ap.add_argument("--quiet", action="store_true", help="only print failures")
    known, cycle_args = ap.parse_known_args()
    # argparse leaves the "--" separator in the extras; run_cycle.py would
    # reject it. Dropping it here is what makes the documented invocation
    #   serve.py --interval 3600 -- --jurisdiction GB --db /data/spine.db
    # work as written.
    if cycle_args and cycle_args[0] == "--":
        cycle_args = cycle_args[1:]

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    print(f"[serve] starting; interval {known.interval}s, cycle args "
          f"{cycle_args or '(none)'}", flush=True)
    write_heartbeat(known.heartbeat, state="starting", passes=0,
                    consecutive_failures=0)

    passes = 0
    failures = 0
    code = 0
    while not _stop:
        started = time.monotonic()
        code, output = run_pass(cycle_args, known.pass_timeout)
        passes += 1
        elapsed = time.monotonic() - started

        if code == 0:
            failures = 0
            if not known.quiet:
                print(f"[serve] pass {passes} ok in {elapsed:.1f}s", flush=True)
        else:
            failures += 1
            print(f"[serve] pass {passes} FAILED (exit {code}, {failures} in a "
                  f"row) in {elapsed:.1f}s", flush=True)
            print(output[-2000:], file=sys.stderr, flush=True)

        write_heartbeat(known.heartbeat,
                        state="ok" if code == 0 else "failing",
                        passes=passes, consecutive_failures=failures,
                        last_exit_code=code, last_duration_seconds=round(elapsed, 2))

        if failures >= known.max_consecutive_failures:
            print(f"[serve] {failures} consecutive failures; exiting so a "
                  "supervisor can restart or a human can look. A loop that "
                  "fails silently forever is worse than one that stops.",
                  flush=True)
            write_heartbeat(known.heartbeat, state="failing", passes=passes,
                            consecutive_failures=failures, last_exit_code=code)
            return 1
        if known.max_passes and passes >= known.max_passes:
            print(f"[serve] reached {known.max_passes} passes; exiting", flush=True)
            break
        if _stop:
            break

        # Sleep in slices so a signal is noticed promptly rather than after the
        # full interval. A container stop that takes an hour gets SIGKILLed.
        deadline = time.monotonic() + known.interval
        while not _stop and time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    write_heartbeat(known.heartbeat, state="stopped", passes=passes,
                    consecutive_failures=failures, last_exit_code=code)
    print(f"[serve] stopped cleanly after {passes} pass(es)", flush=True)
    # A bounded run whose last pass failed has not succeeded, whatever the pass
    # count says. Reporting zero would make a fully broken run indistinguishable
    # from a working one.
    return 1 if code != 0 else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Run every validation suite. Stdlib only, no test framework.

Order is dependency order: schema, then the phases, then the end-to-end
integration last, because it is the only one that can fail for reasons the
others cannot see.
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SUITES = [
    ("schema", "db/test_schema_v2.py"),
    ("phase 1 — ledger, chain, point-in-time", "tests/test_phase1.py"),
    ("phase 2 — scoring, decision, sequential", "tests/test_phase2.py"),
    ("phase 2 — models and ablations", "tests/test_phase2b.py"),
    ("evidence pipeline", "tests/test_evidence.py"),
    ("end-to-end integration", "tests/test_e2e.py"),
]


def main() -> int:
    verbose = "-v" in sys.argv
    failures, total = [], 0
    for label, path in SUITES:
        p = subprocess.run([sys.executable, os.path.join(ROOT, path)],
                           capture_output=True, text=True, cwd=ROOT)
        tail = [ln for ln in p.stdout.splitlines() if "passed" in ln or "behaved" in ln]
        summary = tail[-1] if tail else "(no summary line)"
        mark = "ok  " if p.returncode == 0 else "FAIL"
        print(f"{mark} {label:<42} {summary}")
        if verbose or p.returncode != 0:
            print(p.stdout)
            if p.stderr:
                print(p.stderr, file=sys.stderr)
        if p.returncode != 0:
            failures.append(path)
        total += 1
    print(f"\n{total - len(failures)}/{total} suites passed")
    for f in failures:
        print(f"  failed: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Measure which `raise` sites in `spine/` any suite actually reaches.

A guard no test reaches is a guard nobody has checked. It may be unreachable, it
may have its condition inverted, it may crash on the way to raising — all three
look identical from outside, and all three mean the discipline it encodes is not
enforced.

This is separate from `run_tests.py` because tracing every suite takes minutes,
and a slow check that runs on every commit gets skipped. Run it when guards are
added or removed.

    python3 tests/trace_refusals.py            # report
    python3 tests/trace_refusals.py --update   # rewrite refusal_targets.json
    python3 tests/trace_refusals.py --verify   # fail if targets are unreached

Stdlib only; no coverage library.
"""

from __future__ import annotations

import ast
import json
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TARGETS = ROOT / "tests" / "refusal_targets.json"

# Runs one suite under a line tracer and writes what it touched in spine/.
# atexit rather than a finally block, so a suite that dies still reports what it
# reached — the first version lost a whole suite's coverage to an ImportError
# and silently reported its module as untested.
_TRACER = r'''
import sys, os, runpy, json, pathlib, atexit
covered, root = {}, %r
def tracer(frame, event, arg):
    fn = frame.f_code.co_filename
    if fn.startswith(root) and event == "line":
        covered.setdefault(os.path.basename(fn), set()).add(frame.f_lineno)
    return tracer
out = sys.argv[2]
def dump():
    sys.settrace(None)
    pathlib.Path(out).write_text(json.dumps({k: sorted(v) for k, v in covered.items()}))
atexit.register(dump)
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
sys.settrace(tracer)
try:
    runpy.run_path(sys.argv[1], run_name="__main__")
except SystemExit:
    pass
'''


def raise_sites() -> dict[str, set[int]]:
    sites: dict[str, set[int]] = {}
    for p in sorted((ROOT / "spine").glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and node.exc is not None:
                sites.setdefault(p.name, set()).add(node.lineno)
    return sites


def trace_all(scratch: pathlib.Path) -> tuple[dict[str, set[int]], list[str]]:
    scratch.mkdir(parents=True, exist_ok=True)
    tracer = scratch / "_tracer.py"
    tracer.write_text(_TRACER % (str(ROOT / "spine") + os.sep,))

    suites = [f"tests/{f}" for f in sorted(os.listdir(ROOT / "tests"))
              if f.startswith("test_") and f not in ("test_docs.py",)]
    suites.append("db/test_schema_v2.py")

    merged: dict[str, set[int]] = {}
    incomplete = []
    for s in suites:
        out = scratch / f"cov_{pathlib.Path(s).name}.json"
        if out.exists():
            out.unlink()
        try:
            subprocess.run([sys.executable, str(tracer), str(ROOT / s), str(out)],
                           capture_output=True, cwd=ROOT, timeout=900)
        except subprocess.TimeoutExpired:
            incomplete.append(f"{s} (timeout)")
        if not out.exists():
            incomplete.append(f"{s} (no output)")
            continue
        for k, v in json.loads(out.read_text()).items():
            merged.setdefault(k, set()).update(v)
        print(f"  traced {s}", file=sys.stderr)
    return merged, incomplete


def main() -> int:
    update = "--update" in sys.argv
    verify = "--verify" in sys.argv
    scratch = pathlib.Path(
        os.environ.get("SPINE_SCRATCH", "/tmp/spine-refusal-trace"))

    sites = raise_sites()
    covered, incomplete = trace_all(scratch)

    total = sum(len(v) for v in sites.values())
    missing = {m: sorted(v - covered.get(m, set())) for m, v in sites.items()}
    missing = {m: v for m, v in missing.items() if v}
    n_missing = sum(len(v) for v in missing.values())

    print("=" * 76)
    print("RAISE-SITE COVERAGE")
    print("=" * 76)
    if incomplete:
        print("\nINCOMPLETE — these suites produced no coverage, so the numbers")
        print("below understate what is tested:")
        for s in incomplete:
            print(f"  {s}")
    print(f"\n{total} raise sites in spine/, {n_missing} never reached "
          f"({100 * (total - n_missing) // total}% covered)\n")
    for mod in sorted(missing):
        src = (ROOT / "spine" / mod).read_text(encoding="utf-8").splitlines()
        print(f"  {mod}:")
        for ln in missing[mod]:
            print(f"    {ln:>4}: {src[ln - 1].strip()[:84]}")

    if update:
        TARGETS.write_text(json.dumps(missing, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {TARGETS.relative_to(ROOT)}")
    if verify:
        targets = json.loads(TARGETS.read_text()) if TARGETS.exists() else {}
        unreached = {m: sorted(set(v) & set(missing.get(m, [])))
                     for m, v in targets.items()}
        unreached = {m: v for m, v in unreached.items() if v}
        if unreached:
            print("\nFAIL: targets still unreached by any suite:")
            for m, v in unreached.items():
                print(f"  {m}: {v}")
            return 1
        print("\nOK: every target in refusal_targets.json is reached")
    return 0


if __name__ == "__main__":
    sys.exit(main())

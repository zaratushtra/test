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

    python3 tests/trace_refusals.py            # coverage across all suites
    python3 tests/trace_refusals.py --update   # record what test_refusals covers
    python3 tests/trace_refusals.py --verify   # fail on a gap, or a lost target

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


def raise_sites() -> dict[str, dict[int, str]]:
    """Every raise site, as {module: {line: source text}}."""
    sites: dict[str, dict[int, str]] = {}
    for p in sorted((ROOT / "spine").glob("*.py")):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and node.exc is not None:
                try:
                    text = ast.unparse(node.exc)
                except Exception:  # noqa: BLE001
                    text = "<unparseable>"
                sites.setdefault(p.name, {})[node.lineno] = " ".join(text.split())
    return sites


def trace_all(scratch: pathlib.Path,
              only: str | None = None) -> tuple[dict[str, set[int]], list[str]]:
    scratch.mkdir(parents=True, exist_ok=True)
    tracer = scratch / "_tracer.py"
    tracer.write_text(_TRACER % (str(ROOT / "spine") + os.sep,))

    if only:
        suites = [only]
    else:
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
    # Keyed by the raise TEXT, not the line. Line numbers go stale on the next
    # edit above them, and a stale target list fails for a reason that has
    # nothing to do with coverage -- which this check discovered about itself.
    missing = {m: sorted(text for ln, text in v.items()
                         if ln not in covered.get(m, set()))
               for m, v in sites.items()}
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
        print(f"  {mod}:")
        for text in missing[mod]:
            print(f"    {text[:88]}")

    # What test_refusals.py alone covers. That is the meaningful record: it says
    # which guards THAT suite is responsible for, so deleting one of its checks
    # is caught even while another suite happens to reach the same line.
    own, own_incomplete = trace_all(scratch, only="tests/test_refusals.py")
    owned = {m: sorted(text for ln, text in v.items()
                       if ln in own.get(m, set()))
             for m, v in sites.items()}
    owned = {m: v for m, v in owned.items() if v}
    n_owned = sum(len(v) for v in owned.values())
    print(f"\ntests/test_refusals.py alone reaches {n_owned} of them")

    if update:
        TARGETS.write_text(json.dumps(owned, indent=2, sort_keys=True) + "\n")
        print(f"wrote {TARGETS.relative_to(ROOT)} ({n_owned} guards)")

    rc = 0
    if verify:
        if n_missing:
            print(f"\nFAIL: {n_missing} guard(s) unreached by any suite")
            rc = 1
        targets = json.loads(TARGETS.read_text()) if TARGETS.exists() else {}
        lost = {m: sorted(set(v) - set(owned.get(m, [])))
                for m, v in targets.items()}
        lost = {m: v for m, v in lost.items() if v}
        if lost:
            print("\nFAIL: test_refusals.py no longer reaches guards it owns:")
            for m, v in lost.items():
                for t in v:
                    print(f"  {m}: {t[:80]}")
            rc = 1
        if rc == 0:
            print("\nOK: every guard is reached, and test_refusals.py still "
                  "covers all it owns")
    return rc


if __name__ == "__main__":
    sys.exit(main())

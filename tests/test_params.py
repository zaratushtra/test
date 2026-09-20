#!/usr/bin/env python3
"""
The parameter registry as a gate, not as documentation.

A registry nobody checks is a document that drifts. These checks make it
structural: every module-level constant in `spine/` must be registered with its
provenance, every registered value must match the code, and every parameter
called `declared` must say what would replace it.

The last one is the load-bearing rule. A declared parameter with no replacement
path is indistinguishable from a measurement nobody made.

Run: python3 tests/test_params.py
Stdlib only.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import params  # noqa: E402
from spine.params import DECLARED, DERIVED, EXTERNAL, MEASURED, Param  # noqa: E402

PASS, FAIL = [], []

# Constants that are structural rather than tunable: type tags, SQL fragments,
# genesis sentinels. Registering these would bury the numbers that matter.
EXEMPT_NAMES = {
    # Checked against PRAGMA user_version by tests/test_docs.py. Registering it
    # here would be a second source of truth for one value, which is what this
    # registry exists to prevent -- and it went stale on the first schema bump
    # after being added, which is how that was noticed.
    "SCHEMA_VERSION",
    "COMMITTED_FIELDS", "GENESIS_HASH", "SCHEMA_PATH", "PAPER", "LIVE",
    "TRADEABLE", "BINARY_PAYOUTS", "SCORABLE", "SCORABLE_OUTCOMES",
    "EXCLUSIONS", "STOPWORDS", "SPENDING", "ALL_SLICES", "GAMMA_BASE",
    "CLOB_BASE", "USER_AGENT", "ALL_SCOPES", "CANONICAL", "SQL_GLOB", "MEASURED", "DERIVED",
    "DECLARED", "EXTERNAL", "PARAMS", "BY_WHERE", "CLOSE_ONLY_JURISDICTIONS",
    "FIELD_CANDIDATES", "MIN_WIDTH", "Z", "NS",
}


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


def module_constants() -> dict[str, tuple[str, object]]:
    """Module-level UPPER_CASE assignments across spine/, by qualified name."""
    found = {}
    for path in sorted(pathlib.Path(ROOT, "spine").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if not name.isupper() or name.startswith("_"):
                    continue
                if name in EXEMPT_NAMES:
                    continue
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    value = "<computed>"
                found[f"spine/{path.stem}.{name}"] = (name, value)
    return found


def main() -> int:
    print("=" * 76)
    print("SPINE PARAMETER REGISTRY")
    print("=" * 76)

    print("\n[1] Every constant in spine/ is registered\n")
    found = module_constants()
    ok("constants were discovered", len(found) > 5, len(found))
    missing = sorted(set(found) - set(params.BY_WHERE))
    ok("no constant is unregistered", not missing, missing)
    stale = sorted(w for w in params.BY_WHERE
                   if w.startswith("spine/") and "(" not in w
                   and w not in found)
    ok("no registered constant has vanished from the code", not stale, stale)
    print(f"        {len(found)} constants discovered, "
          f"{len(params.PARAMS)} registered in total")

    print("\n[2] Registered values match the code\n")
    mismatched = []
    for where, (_, value) in found.items():
        p = params.BY_WHERE.get(where)
        if p is None or value == "<computed>":
            continue
        if isinstance(value, (int, float)) and p.value != value:
            mismatched.append((where, p.value, value))
    ok("no registered value disagrees with the constant it describes",
       not mismatched, mismatched)

    import spine.evidence as ev
    import spine.scoring as sc
    ok("the measured minimum-regime count is the one in scoring",
       params.BY_WHERE["spine/scoring.MIN_REGIMES"].value == sc.MIN_REGIMES)
    ok("the assumed correlation is the one evidence actually falls back to",
       params.BY_WHERE["spine/evidence.ASSUMED_CORRELATION"].value
       == ev.ASSUMED_CORRELATION)

    print("\n[3] Policy defaults are registered too, and still are the defaults\n")
    import inspect

    import spine.collect as co
    import spine.decision as de
    import spine.models as mo
    import spine.venue as ve
    for where, fn, arg in [
        ("spine/decision.decide(min_edge_bp=)", de.decide, "min_edge_bp"),
        ("spine/models.independent_forecast(widen_per_contribution_bp=)",
         mo.independent_forecast, "widen_per_contribution_bp"),
        ("spine/evidence.cluster_items(duplicate_threshold=)",
         ev.cluster_items, "duplicate_threshold"),
        ("spine/evidence.cluster_items(topic_threshold=)",
         ev.cluster_items, "topic_threshold"),
        ("spine/evidence.cluster_items(window_hours=)",
         ev.cluster_items, "window_hours"),
        ("spine/collect.run_query(verification_lag_seconds=)",
         co.run_query, "verification_lag_seconds"),
        ("spine/venue.snapshot_books(capture_lag_seconds=)",
         ve.snapshot_books, "capture_lag_seconds"),
    ]:
        actual = inspect.signature(fn).parameters[arg].default
        ok(f"{arg} default matches the registry",
           params.BY_WHERE[where].value == actual,
           (params.BY_WHERE[where].value, actual))

    print("\n[4] A declared parameter must say what would replace it\n")
    for p in params.unsupported():
        ok(f"{p.where} names its replacement", bool(p.replaced_by.strip()))
    ok("constructing a declared parameter without one is refused",
       raises(lambda: Param("x", 1, "y", DECLARED, "because"), ValueError))
    ok("...and the message says why that matters",
       "indistinguishable from a measurement nobody made" in _msg(
           lambda: Param("x", 1, "y", DECLARED, "because")))
    ok("an unknown provenance is refused",
       raises(lambda: Param("x", 1, "y", "vibes", "because"), ValueError))

    print("\n[5] The project's own list of what it takes on faith\n")
    decl = params.unsupported()
    ok("there are declared parameters, and they are visible", len(decl) > 5,
       len(decl))
    ok("the measured ones cite the analysis that produced them",
       all(("phase1/" in p.justification or "phase0/" in p.justification)
           for p in params.PARAMS if p.provenance == MEASURED))
    ok("the derived ones state the identity they follow from",
       all(len(p.justification) > 40
           for p in params.PARAMS if p.provenance == DERIVED))
    ok("the external ones carry a date, because those expire",
       all(("2026" in p.justification or "schema" in p.justification)
           for p in params.PARAMS if p.provenance == EXTERNAL))
    ok("the report renders", len(params.report().splitlines()) > 10)
    print()
    print("        " + params.report().splitlines()[-1])
    print(f"        {len(decl)} of {len(params.PARAMS)} are taken on faith, "
          "each naming what would settle it")

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

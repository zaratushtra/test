#!/usr/bin/env python3
"""
Timestamp canonicalisation: the bug, and the guard.

Every point-in-time query in this project is a string comparison. This suite
exists because that was silently false for any row written on a whole second.

Run: python3 tests/test_timeutil.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import timeutil as t  # noqa: E402
from spine.timeutil import TimeError  # noqa: E402

PASS, FAIL = [], []


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
    print("SPINE TIMESTAMP CANONICALISATION")
    print("=" * 76)

    print("\n[1] The bug this module exists for\n")
    whole = "2026-09-19T13:00:00Z"
    milli = "2026-09-19T13:00:00.000Z"
    ok("the two strings denote the same instant", t.parse(whole) == t.parse(milli))
    ok("...but the whole-second form sorts AFTER the millisecond form",
       not (whole <= milli) and (milli <= whole))
    ok("so a row stamped the first way is invisible to a cutoff of the second",
       not (whole <= milli))
    ok("canonicalising both makes the comparison true again",
       t.canonical(whole) <= t.canonical(milli))
    print(f"        {whole!r} <= {milli!r}  ->  {whole <= milli}")
    print(f"        after canonical():                          -> "
          f"{t.canonical(whole) <= t.canonical(milli)}")

    print("\n[2] One shape out, many shapes in\n")
    for raw, want in [
        ("2026-09-19T13:00:00Z", "2026-09-19T13:00:00.000Z"),
        ("2026-09-19T14:00:00+01:00", "2026-09-19T13:00:00.000Z"),
        ("2026-09-19T13:00:00.123456Z", "2026-09-19T13:00:00.123Z"),
        ("2026-09-19T13:00:00.5Z", "2026-09-19T13:00:00.500Z"),
        ("2026-09-19T13:00:00", "2026-09-19T13:00:00.000Z"),
        ("2026-09-19t13:00:00z", "2026-09-19T13:00:00.000Z"),
        ("  2026-09-19T13:00:00Z  ", "2026-09-19T13:00:00.000Z"),
    ]:
        ok(f"{raw.strip()!r} canonicalises", t.canonical(raw) == want,
           t.canonical(raw))
    ok("canonicalising is idempotent",
       t.canonical(t.canonical("2026-09-19T13:00:00Z")) == "2026-09-19T13:00:00.000Z")
    ok("a naive datetime is treated as UTC rather than local",
       t.iso(datetime(2026, 9, 19, 13)) == "2026-09-19T13:00:00.000Z")
    ok("an aware datetime is converted, not relabelled",
       t.iso(datetime(2026, 9, 19, 14,
                      tzinfo=timezone(timedelta(hours=1))))
       == "2026-09-19T13:00:00.000Z")

    print("\n[3] Refusals\n")
    ok("nonsense is refused", raises(lambda: t.parse("whenever"), TimeError))
    ok("an empty string is refused", raises(lambda: t.parse(""), TimeError))
    ok("None is refused", raises(lambda: t.parse(None), TimeError))
    ok("a number is refused", raises(lambda: t.parse(1758283200), TimeError))
    ok("is_canonical is strict about the whole-second form",
       not t.is_canonical("2026-09-19T13:00:00Z"))
    ok("is_canonical is strict about microseconds",
       not t.is_canonical("2026-09-19T13:00:00.000000Z"))
    ok("is_canonical is strict about offsets",
       not t.is_canonical("2026-09-19T13:00:00.000+00:00"))
    ok("is_canonical accepts the canonical form",
       t.is_canonical("2026-09-19T13:00:00.000Z"))
    ok("require_canonical names the field it rejected",
       "created_at" in _msg(lambda: t.require_canonical("nope", "created_at")))
    ok("...and explains that comparisons are lexicographic",
       "lexicographic" in _msg(lambda: t.require_canonical("nope")))

    print("\n[4] The regex and the SQL GLOB agree\n")
    import re
    # Translate the GLOB into a regex and check the two accept the same strings.
    glob_re = re.compile("^" + t.SQL_GLOB.replace("[0-9]", r"\d").replace(".", r"\.")
                         + "$")
    cases = ["2026-09-19T13:00:00.000Z", "2026-09-19T13:00:00Z",
             "2026-09-19T13:00:00.000000Z", "2026-09-19T13:00:00.000+00:00",
             "0000-00-00T00:00:00.000Z", "x"]
    for c in cases:
        ok(f"{c!r} agrees between regex and GLOB",
           bool(t.CANONICAL.match(c)) == bool(glob_re.match(c)),
           (bool(t.CANONICAL.match(c)), bool(glob_re.match(c))))

    print("\n[5] now() is canonical, always\n")
    stamps = [t.now() for _ in range(200)]
    ok("200 consecutive stamps are all canonical",
       all(t.is_canonical(s) for s in stamps),
       next((s for s in stamps if not t.is_canonical(s)), None))
    ok("...and they are non-decreasing as strings",
       all(a <= b for a, b in zip(stamps, stamps[1:])))
    # The original bug would only appear on the ~1-in-1000 whole-millisecond
    # tick, which is exactly why it survived so long.
    ok("a whole-second instant stamps canonically",
       t.is_canonical(t.iso(datetime(2026, 9, 19, 13, tzinfo=timezone.utc))))

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

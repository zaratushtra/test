#!/usr/bin/env python3
"""
Documentation consistency: the claims in the docs, checked against the code.

This project's documents make a lot of specific, checkable assertions — suite
counts, file layouts, schema versions, measured figures. They have been edited
many times. Every one of them is a claim that can quietly become false, and a
document that is confidently wrong is worse than one that says nothing, because
it gets believed.

So the checkable ones are checked: suite counts against what the suites actually
report, the schema version against the schema file, every path a document names
against the filesystem, and the figures that are cited as reasons for design
decisions against the constants they came from.

**This suite is not part of `run_tests.py`, and cannot be.** It invokes
`run_tests.py` to learn the real counts, so including it would recurse. That is
the one honest reason to keep a test out of the suite runner, and it is why
`run_tests.py --docs` exists instead.

Run: python3 tests/test_docs.py   (or python3 run_tests.py --docs)
Stdlib only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger  # noqa: E402

PASS, FAIL = [], []


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def main() -> int:
    print("=" * 76)
    print("SPINE DOCUMENTATION CONSISTENCY")
    print("=" * 76)

    readme = read("README.md")
    design = read("SPINE-DESIGN-V2.md")
    running = read("docs/RUNNING.md")
    schema = read("db/schema_v2.sql")

    # ---------------------------------------------------- schema version
    print("\n[1] The schema version is claimed in two places\n")
    m = re.search(r"PRAGMA user_version = (\d+);", schema)
    ok("the schema file stamps a version", m is not None)
    ok("ledger.SCHEMA_VERSION matches the schema file",
       m and int(m.group(1)) == ledger.SCHEMA_VERSION,
       (m.group(1) if m else None, ledger.SCHEMA_VERSION))
    versions = re.findall(r"^#\s+(\d+)\s+\S", read("spine/ledger.py"), re.MULTILINE)
    ok("every version up to the current one has a changelog line",
       versions and int(versions[-1]) == ledger.SCHEMA_VERSION,
       (versions, ledger.SCHEMA_VERSION))

    # ---------------------------------------------------- suite counts
    print("\n[2] The suite counts the documents quote\n")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "run_tests.py")],
                       capture_output=True, text=True, cwd=ROOT)
    ok("run_tests.py passes", r.returncode == 0, r.stdout[-400:])
    suites = re.findall(r"(\d+)/(\d+) (?:checks passed|probes behaved)", r.stdout)
    n_suites = len(suites)
    n_checks = sum(int(a) for a, _ in suites)
    ok("every suite reported a count", n_suites > 0)
    ok("every check in every suite passed",
       all(a == b for a, b in suites), suites)
    print(f"        actual: {n_suites} suites, {n_checks} checks")

    for name, text in (("README.md", readme), ("SPINE-DESIGN-V2.md", design),
                       ("docs/RUNNING.md", running)):
        claims = re.findall(r"(\d+)\s+suites?,\s+(\d+)\s+checks", text)
        claims += [(b, a) for a, b in
                   re.findall(r"(\d+)\s+checks across\s+(\d+)\s+suites", text)]
        if not claims:
            continue
        for cs, cc in claims:
            ok(f"{name} claims {cs} suites and {cc} checks",
               int(cs) == n_suites and int(cc) == n_checks,
               f"actual {n_suites}/{n_checks}")

    # ---------------------------------------------------- layout
    print("\n[3] Every path the README lists exists\n")
    layout = re.search(r"## Layout\n\n```\n(.*?)```", readme, re.DOTALL)
    ok("the README has a layout block", layout is not None)
    missing = []
    if layout:
        for line in layout.group(1).splitlines():
            path = line.split()[0] if line.strip() else ""
            if not path or path.endswith("/"):
                if path and not os.path.isdir(os.path.join(ROOT, path)):
                    missing.append(path)
                continue
            if not os.path.exists(os.path.join(ROOT, path)):
                missing.append(path)
    ok("no listed path is missing", not missing, missing)

    print("\n[4] Every document referenced by another exists\n")
    refs = set()
    for text in (readme, design, running, read("docs/EVALUATION-REPORT.md"),
                 read("docs/EVIDENCE-REPORT.md"),
                 read("docs/SHADOW-EXECUTION-REPORT.md")):
        refs |= set(re.findall(r"`((?:docs/|phase\d/|spine/|tests/|db/)[\w./-]+)`",
                               text))
        refs |= set(re.findall(r"\]\(([\w./-]+\.md)\)", text))
    broken = sorted(p for p in refs if not os.path.exists(os.path.join(ROOT, p)))
    ok("no referenced file is missing", not broken, broken)
    print(f"        checked {len(refs)} referenced paths")

    # ---------------------------------------------------- runnable commands
    print("\n[5] Every `python3 ...` the docs tell you to run is runnable\n")
    cmds = set()
    for text in (readme, running):
        cmds |= set(re.findall(r"python3 ([\w./-]+\.py)", text))
    bad = sorted(c for c in cmds if not os.path.exists(os.path.join(ROOT, c)))
    ok("every script the docs name exists", not bad, bad)
    ok("run_tests.py lists every suite file that exists",
       set(re.findall(r'"(tests/[\w]+\.py|db/[\w]+\.py)"', read("run_tests.py")))
       >= {f"tests/{f}" for f in os.listdir(os.path.join(ROOT, "tests"))
           if f.startswith("test_") and f != "test_docs.py"},
       sorted(set(f"tests/{f}" for f in os.listdir(os.path.join(ROOT, "tests"))
                  if f.startswith("test_"))
              - set(re.findall(r'"(tests/[\w]+\.py)"', read("run_tests.py")))))
    print(f"        checked {len(cmds)} commands")

    # ---------------------------------------------------- posture
    print("\n[6] The paper-only posture is a property of the code\n")
    venue = read("spine/venue.py")
    for token in ("Authorization", "api_key", "private_key", "secret", "sign("):
        ok(f"the venue client has no {token!r}", token not in venue)
    ok("there is no order-management module",
       not os.path.exists(os.path.join(ROOT, "spine", "order_manager.py")))
    ok("the design says live trading is out of scope",
       "out of scope" in design and "paper only" in design.lower())
    ok("README states the paper-only posture prominently",
       "Paper only" in readme or "paper only" in readme.lower())

    # ---------------------------------------------------- measured figures
    print("\n[6b] Every section reference points at a section that exists\n")
    # A reference by number survives a rewrite looking exactly as authoritative
    # as it did before. Three in this repo pointed at v1 section numbers, which
    # in v2 mean entirely different things -- and nothing could see it, because
    # they were in code comments rather than prose.
    heads = {m.group(1) for m in
             re.finditer(r"^#{2,4}\s+(\d+[a-z]?(?:\.\d+)?)\.?\s", design,
                         re.MULTILINE)}
    ok("the design has numbered sections to check against", len(heads) > 20,
       len(heads))

    dangling = {}
    for path in sorted(
            list(__import__("pathlib").Path(ROOT).rglob("*.py")) +
            list(__import__("pathlib").Path(ROOT).rglob("*.md"))):
        sp = str(path)
        if "__pycache__" in sp or "/docs/history/" in sp:
            continue
        text = path.read_text(encoding="utf-8")
        # A document about v1 legitimately cites v1 numbers -- but it has to say
        # so, because an unlabelled section number is exactly as ambiguous to a
        # reader as it is to this check.
        if "Section references in this document are to SPINE v1" in text:
            continue
        # A reference resolves against the design's sections OR the document's
        # own -- a report citing its own section 2.4 is not a dangling link.
        own = {m.group(1) for m in
               re.finditer(r"^#{2,4}\s+(\d+[a-z]?(?:\.\d+)?)\.?\s", text,
                           re.MULTILINE)}
        refs = set(re.findall(r"§(\d+(?:\.\d+)?)", text))
        bad = sorted(refs - heads - own)
        if bad:
            dangling[os.path.relpath(sp, ROOT)] = bad
    ok("no file references a section the design does not have", not dangling,
       dangling)
    print(f"        checked {len(heads)} sections across the repo")

    print("\n[7] Figures the documents quote as measured\n")
    # These are load-bearing: every one is cited as a reason for a design choice.
    for doc, name, figure in (
        (design, "SPINE-DESIGN-V2.md", "19.3%"),      # peeking inflation
        (design, "SPINE-DESIGN-V2.md", "31.4"),       # power coefficient
        (readme, "README.md", "0.061"),               # lexical overlap
        (read("docs/EVIDENCE-REPORT.md"), "EVIDENCE-REPORT.md", "0.30"),
        (read("docs/EVALUATION-REPORT.md"), "EVALUATION-REPORT.md", "6.09"),
    ):
        ok(f"{name} still quotes {figure}", figure in doc)
    import phase0.screen_markets as sm  # noqa: E402
    sys.path.insert(0, os.path.join(ROOT, "phase0"))
    ok("the power coefficient in code is the corrected 31.4",
       sm.POWER_COEFFICIENT == 31.4, sm.POWER_COEFFICIENT)
    from spine.scoring import MIN_REGIMES  # noqa: E402
    ok("MIN_REGIMES is the twelve the documents quote", MIN_REGIMES == 12)
    ok("...and the design says twelve", "12 regimes" in design or
       "≥12" in design or "twelve regimes" in design.lower())

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

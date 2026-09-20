#!/usr/bin/env python3
"""
Check `spine/canonical.py` against a second implementation of RFC 8785.

The design document's argument for content-addressing is that **somebody else
can check it**: §9.1 anchors the hash chain externally so pre-registration is a
claim a third party need believe rather than take on trust. That argument is
worth exactly as much as the claim of RFC 8785 conformance underneath it, and
until now that claim was checked by eleven hand-written number cases and a
handful of key-ordering examples — all written by the same person who wrote the
code, from the same reading of the spec. A shared misreading would pass both.

So this compares against an implementation that shares no reading at all. RFC
8785 does not define number and string formatting itself; it *delegates* to
ECMAScript — `Number::toString` for numbers, `JSON.stringify`'s string
serialisation for strings, UTF-16 code unit order for keys. A JavaScript engine
is therefore not a second opinion about the spec, it is the thing the spec
points at. Node's V8 is used as that authority.

    python3 tests/interop_jcs.py              # 50k numbers + 6k payloads
    python3 tests/interop_jcs.py --quick      # a tenth of each, for a commit

Exits non-zero on any disagreement. Exits **zero with a stated skip** when node
is absent: this is an interop check, not a correctness gate, and a machine
without a JavaScript engine has not found a bug. The skip says so out loud
rather than passing quietly, because a check that silently does nothing is worse
than one that is not run.

Not part of `run_tests.py` — it needs a second language runtime, which the rest
of this project does not.
"""

from __future__ import annotations

import json
import os
import pathlib
import random
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from spine import params  # noqa: E402
from spine.canonical import _es6_number, canonicalise, content_hash  # noqa: E402

# The reference. Deliberately thin: every interesting decision is delegated to a
# built-in, because the built-ins are what RFC 8785 actually cites.
#
#   numbers -> String(x), which is ECMAScript Number::toString
#   strings -> JSON.stringify, whose string serialisation RFC 8785 adopts whole
#   keys    -> `<` on strings, which in JS compares UTF-16 code units
#
# Writing a *careful* reference here would defeat the purpose: the point is that
# it embodies a different reading, not a more diligent one.
REFERENCE_JS = r"""
function jcs(v) {
  if (v === null) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") {
    if (!isFinite(v)) throw new Error("non-finite number");
    return Object.is(v, -0) ? "0" : String(v);
  }
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(jcs).join(",") + "]";
  if (typeof v === "object") {
    const keys = Object.keys(v).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
    return "{" + keys.map(k => JSON.stringify(k) + ":" + jcs(v[k])).join(",") + "}";
  }
  throw new Error("unsupported type " + typeof v);
}

const crypto = require("crypto");
const fs = require("fs");
const mode = process.argv[2];
const rows = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

if (mode === "numbers") {
  // Doubles cross the boundary as their exact bit pattern, in a STRING. Sent as
  // a JSON number, a bit pattern above 2**53 is silently rounded by JSON.parse
  // and every comparison downstream comes out wrong -- which is what happened
  // on the first run of this check, producing 46665 spurious mismatches.
  const dv = new DataView(new ArrayBuffer(8));
  console.log(JSON.stringify(rows.map(bits => {
    dv.setBigUint64(0, BigInt(bits), true);
    return String(dv.getFloat64(0, true));
  })));
} else {
  console.log(JSON.stringify(rows.map(p => {
    try {
      const text = jcs(p);
      return {text, hash: crypto.createHash("sha256").update(text, "utf8").digest("hex")};
    } catch (e) {
      return {error: String(e.message)};
    }
  })));
}
"""

PASS: list[str] = []
FAIL: list[str] = []


def ok(label: str, cond: bool, detail: object = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail and not cond else ""))


def node() -> str | None:
    return shutil.which("node") or shutil.which("nodejs")


def _run(exe: str, script: pathlib.Path, mode: str, payload_file: pathlib.Path) -> list:
    out = subprocess.run([exe, str(script), mode, str(payload_file)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"reference implementation failed: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout)


# ---------------------------------------------------------------------------
# corpora
# ---------------------------------------------------------------------------

def number_corpus(n: int, rnd: random.Random) -> list[float]:
    """Spec boundaries first, then three kinds of random."""
    vals = [
        # The thresholds in ES6's Number::toString, from both sides.
        1e21, 1e20, 1e-6, 1e-7, 1e22, 1e23,
        # Integral floats, which is where `repr` and ES6 disagreed.
        1.0, 100.0, 900.0, -0.0, 0.5, 0.25,
        # The representable extremes, including the subnormal floor.
        5e-324, 4.9e-324, 1e-323, 2.2250738585072014e-308,
        2.225073858507201e-308, 1.7976931348623157e308,
        # Where 64-bit integers stop being exactly representable.
        9007199254740992.0, 9007199254740993.0, 1234567890123456789.0,
        # Values this project actually computes.
        0.1, 0.3, 0.1 + 0.2, 1 / 3, 2 / 3, 31.4, 0.061, 0.30000000000000004,
    ]
    while len(vals) < n // 2:
        x = struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(64)))[0]
        if x == x and abs(x) != float("inf"):
            vals.append(x)                       # uniform over the bit space
    while len(vals) < n * 3 // 4:
        vals.append(rnd.randint(-10**6, 10**6) / 10 ** rnd.randrange(0, 13))
    while len(vals) < n:
        vals.append(rnd.randint(1, 10**9) * 10.0 ** rnd.randrange(-30, 31))
    return vals


# Characters chosen for the places two implementations can differ, not for
# looking like text: the control range that must become \u00xx, the seven
# characters with short escapes, the line separators JSON leaves unescaped but
# JavaScript source cannot contain, and astral characters, whose surrogate pairs
# are the entire reason keys sort by code unit rather than code point.
ALPHABET = (
    [chr(c) for c in range(0x20, 0x7F)]
    + [chr(c) for c in range(0x00, 0x20)]
    + list('"\\/\b\f\n\r\t')
    + ["é", "ü", "ß", "中", "日", "ý", "а", "￿", "�"]
    + [" ", " ", "‮", "​", "﻿"]
    + ["\U0001F600", "\U0001F4A9", "\U00010000", "\U0010FFFF"]
)


def payload_corpus(n: int, rnd: random.Random) -> list:
    def rstr(maxlen=12):
        return "".join(rnd.choice(ALPHABET) for _ in range(rnd.randrange(0, maxlen)))

    def rnum():
        k = rnd.randrange(4)
        if k == 0:
            # Bounded by 2**53: beyond it JSON.parse cannot read back what
            # Python wrote, and the disagreement would be the transport's.
            return rnd.randint(-2**53 + 1, 2**53 - 1)
        if k == 1:
            return rnd.randint(-10**6, 10**6) / 10 ** rnd.randrange(0, 10)
        if k == 2:
            return rnd.randint(1, 10**9) * 10.0 ** rnd.randrange(-25, 25)
        while True:
            x = struct.unpack("<d", struct.pack("<Q", rnd.getrandbits(64)))[0]
            if x == x and abs(x) != float("inf"):
                return x

    def rval(d=0):
        if d >= 4:
            return rnd.choice([None, True, False, rstr(), rnum()])
        k = rnd.randrange(8)
        if k == 0:
            return None
        if k == 1:
            return rnd.choice([True, False])
        if k == 2:
            return rstr()
        if k in (3, 4):
            return rnum()
        if k == 5:
            return [rval(d + 1) for _ in range(rnd.randrange(0, 5))]
        return {rstr(8): rval(d + 1) for _ in range(rnd.randrange(0, 6))}

    rows = [{rstr(8): rval(1) for _ in range(rnd.randrange(1, 7))} for _ in range(n - 1)]
    # And the payload every single forecast in this project actually commits to.
    rows.append(params.snapshot())
    return rows


# ---------------------------------------------------------------------------

def main() -> int:
    quick = "--quick" in sys.argv
    n_numbers = 5_000 if quick else 50_000
    n_payloads = 600 if quick else 6_000

    exe = node()
    print("=" * 76)
    print("RFC 8785 INTEROP: spine/canonical.py against ECMAScript")
    print("=" * 76)
    if exe is None:
        print("\nSKIPPED: no `node` on PATH.\n")
        print("  This check needs a JavaScript engine because RFC 8785 delegates")
        print("  number and string formatting to ECMAScript; without one there is")
        print("  nothing to compare against. Nothing was checked and nothing")
        print("  failed. Install node, or run this on a machine that has one.")
        return 0
    ver = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()
    print(f"\nreference: {exe} {ver}\n")

    rnd = random.Random(20260920)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="jcs-interop-"))
    try:
        script = tmp / "ref.js"
        script.write_text(REFERENCE_JS, encoding="utf-8")

        print(f"[1] Number::toString over {n_numbers} doubles\n")
        vals = number_corpus(n_numbers, rnd)
        bits = [str(struct.unpack("<Q", struct.pack("<d", v))[0]) for v in vals]
        (tmp / "nums.json").write_text(json.dumps(bits), encoding="utf-8")
        js_nums = _run(exe, script, "numbers", tmp / "nums.json")
        bad = [(v, a, b) for v, a, b in
               ((v, _es6_number(v), j) for v, j in zip(vals, js_nums)) if a != b]
        ok(f"every one of {len(vals)} doubles renders identically", not bad,
           f"{len(bad)} differ, e.g. {bad[:3]}")
        print(f"        {len(vals)} compared, {len(bad)} mismatched")

        print(f"\n[2] Whole payloads over {n_payloads} random documents\n")
        payloads = payload_corpus(n_payloads, rnd)
        (tmp / "payloads.json").write_text(
            json.dumps(payloads, ensure_ascii=True), encoding="utf-8")
        js_rows = _run(exe, script, "payloads", tmp / "payloads.json")

        errs = [r["error"] for r in js_rows if "error" in r]
        ok("the reference serialised every payload", not errs, errs[:3])

        text_bad, hash_bad, examples = 0, 0, []
        for p, j in zip(payloads, js_rows):
            if "error" in j:
                continue
            mine, mine_h = canonicalise(p), content_hash(p)
            if j["text"] != mine:
                text_bad += 1
                if len(examples) < 3:
                    examples.append((mine[:120], j["text"][:120]))
            if j["hash"] != mine_h:
                hash_bad += 1
        ok(f"all {len(payloads)} canonical forms are byte-identical",
           text_bad == 0, f"{text_bad} differ: {examples}")
        ok("...and every SHA-256 digest agrees", hash_bad == 0, hash_bad)
        print(f"        {len(payloads)} payloads, {text_bad} text and "
              f"{hash_bad} digest mismatches")
        print("        (astral keys, control characters, U+2028/9, a BOM, a bidi")
        print("         override, subnormals, and the live parameter snapshot)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  - {f}")
        print("\nA disagreement here is not cosmetic. It means a third party")
        print("reimplementing RFC 8785 computes different digests for this")
        print("project's own records, and concludes they were tampered with.")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

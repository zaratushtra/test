#!/usr/bin/env python3
"""
Check `spine/tsa.py` against OpenSSL, which did not read RFC 3161 with us.

`tests/test_tsa.py` feeds the parser tokens built by
`tests/fixtures/rfc3161_fixture.py`. Both were written here, from one reading of
the spec, so a shared misunderstanding would pass every check in that suite —
the same weakness the canonicalisation tests had before `tests/interop_jcs.py`
put them against ECMAScript.

So this runs the comparison in both directions:

  * **OpenSSL issues, we read.** A throwaway TSA key and certificate are
    generated, `openssl ts` produces a genuine signed timestamp token over known
    data, and `spine/tsa.py` must recover the imprint, genTime, serial and
    policy that `openssl ts -reply -text` reports for the same file.

  * **We issue, OpenSSL reads.** Tokens from the fixture builder are handed to
    `openssl ts -reply -text`, which must report the fields we put in. This is
    what makes the fixture usable as test input: it is not trusted because it
    was written carefully, it is trusted because an independent implementation
    reads it the same way.

    python3 tests/interop_rfc3161.py

Exits non-zero on disagreement, and **zero with a stated skip** when `openssl`
is absent — a machine without it has not found a bug. Outside `run_tests.py`
because it shells out to another toolchain, same as the JCS interop check.

No key material is committed. The TSA is generated into a temporary directory
and destroyed with it; a private key in a repository is a bad habit even when
the key is worthless.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import rfc3161_fixture as fix  # noqa: E402
from spine import tsa  # noqa: E402

PASS, FAIL = [], []

TSA_CNF = """\
[ req ]
distinguished_name = dn
prompt = no
x509_extensions = v3
[ dn ]
CN = SPINE interop test TSA
[ v3 ]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,timeStamping
subjectKeyIdentifier = hash
[ tsa_config ]
serial = serial.txt
signer_cert = tsa.crt
signer_key = tsa.key
signer_digest = sha256
default_policy = 1.3.6.1.4.1.13762.3
digests = sha256,sha512
accuracy = secs:1
ordering = yes
tsa_name = yes
ess_cert_id_chain = no
"""


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def run(args, cwd, env=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    if p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed:\n{p.stderr[-800:]}")
    return p.stdout


# `openssl ts -reply -text` prints a human report. These pull the same four
# facts out of it that spine/tsa.py extracts from the DER, so the comparison is
# between two readings of one file rather than between two of our own functions.
def parse_openssl_text(text: str) -> dict:
    out = {}
    m = re.search(r"Policy OID:\s*(\S+)", text)
    if m:
        out["policy"] = m.group(1)
    m = re.search(r"Serial number:\s*0x([0-9A-Fa-f]+)", text)
    if m:
        out["serial"] = int(m.group(1), 16)
    m = re.search(r"Time stamp:\s*(.+)", text)
    if m:
        out["time"] = m.group(1).strip()
    m = re.search(r"Hash Algorithm:\s*(\S+)", text)
    if m:
        out["alg"] = m.group(1)
    # "Message data:" is followed by an offset/hex/ascii dump.
    body = text.split("Message data:", 1)
    if len(body) == 2:
        digest = ""
        for line in body[1].splitlines():
            m = re.match(r"\s*[0-9a-f]{4} - ((?:[0-9a-f]{2}[ -]){1,16})", line)
            if not m:
                if digest:
                    break
                continue
            digest += m.group(1).replace(" ", "").replace("-", "")
        out["digest"] = digest
    return out


def main() -> int:
    print("=" * 76)
    print("RFC 3161 INTEROP: spine/tsa.py against OpenSSL")
    print("=" * 76)

    exe = shutil.which("openssl")
    if exe is None:
        print("\nSKIPPED: no `openssl` on PATH.\n")
        print("  This check needs a second RFC 3161 implementation to compare")
        print("  against. Without one there is nothing to compare. Nothing was")
        print("  checked and nothing failed.")
        return 0
    ver = run([exe, "version"], cwd=ROOT).strip()
    print(f"\nreference: {exe} — {ver}\n")

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tsa-interop-"))
    try:
        (tmp / "tsa.cnf").write_text(TSA_CNF, encoding="utf-8")
        (tmp / "serial.txt").write_text("01\n", encoding="utf-8")
        payload = b"SPINE interop payload"
        (tmp / "data.bin").write_bytes(payload)
        want_digest = hashlib.sha256(payload).hexdigest()

        print("[1] OpenSSL issues a token, we read it\n")
        run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", "tsa.key", "-out", "tsa.crt", "-days", "3650",
             "-config", "tsa.cnf"], cwd=tmp)
        run([exe, "ts", "-query", "-data", "data.bin", "-sha256", "-cert",
             "-out", "req.tsq"], cwd=tmp)
        run([exe, "ts", "-reply", "-section", "tsa_config",
             "-queryfile", "req.tsq", "-out", "resp.tsr"], cwd=tmp,
            env={"OPENSSL_CONF": str(tmp / "tsa.cnf")})

        der = (tmp / "resp.tsr").read_bytes()
        ok("OpenSSL produced a token", len(der) > 200, len(der))

        # OpenSSL's own verification, so the token is known good before we
        # claim anything about reading it.
        verify_out = subprocess.run(
            [exe, "ts", "-verify", "-data", "data.bin", "-in", "resp.tsr",
             "-CAfile", "tsa.crt", "-untrusted", "tsa.crt"],
            cwd=tmp, capture_output=True, text=True)
        ok("...and verifies it, signature and all",
           "Verification: OK" in verify_out.stdout + verify_out.stderr,
           (verify_out.stdout + verify_out.stderr)[-200:])

        mine = tsa.parse(der)
        theirs = parse_openssl_text(
            run([exe, "ts", "-reply", "-in", "resp.tsr", "-text"], cwd=tmp))

        ok("the message imprint agrees", mine.hashed_message == theirs["digest"],
           f"{mine.hashed_message} vs {theirs.get('digest')}")
        ok("...and is the digest of the data OpenSSL was given",
           mine.hashed_message == want_digest, want_digest)
        ok("the serial agrees", mine.serial == theirs["serial"],
           f"{mine.serial} vs {theirs.get('serial')}")
        ok("the policy OID agrees", mine.policy_oid == theirs["policy"],
           f"{mine.policy_oid} vs {theirs.get('policy')}")
        ok("the hash algorithm agrees",
           theirs["alg"] == "sha256" and mine.hash_algorithm_oid == tsa.SHA256_OID,
           theirs.get("alg"))
        # OpenSSL prints "Sep 20 14:30:57 2026 GMT"; ours is canonical ISO. The
        # comparison is on the instant, not the spelling.
        import datetime
        their_dt = datetime.datetime.strptime(
            theirs["time"].replace(" GMT", ""), "%b %d %H:%M:%S %Y")
        ok("genTime agrees to the second",
           mine.gen_time.startswith(their_dt.strftime("%Y-%m-%dT%H:%M:%S")),
           f"{mine.gen_time} vs {theirs.get('time')}")
        ok("binding against the real digest verifies",
           tsa.verify(der, want_digest).verified_binding)

        print("\n[2] We issue a token, OpenSSL reads it\n")
        # `-token_in` tells OpenSSL the file is a bare TimeStampToken rather
        # than a TimeStampResp. Both forms occur in the wild and spine/tsa.py
        # accepts either, so both are put in front of OpenSSL.
        cases = [
            ("a plain token", fix.token(want_digest), want_digest, 1, True),
            ("a full response", fix.response("ab" * 32), "ab" * 32, 1, False),
            ("a large serial", fix.token("cd" * 32, serial=123456789),
             "cd" * 32, 123456789, True),
        ]
        for i, (label, blob, digest, serial, token_in) in enumerate(cases):
            path = tmp / f"gen-{i}.tsr"
            path.write_bytes(blob)
            argv = [exe, "ts", "-reply", "-in", path.name, "-text"]
            if token_in:
                argv.insert(3, "-token_in")
            text = run(argv, cwd=tmp)
            theirs = parse_openssl_text(text)
            ok(f"OpenSSL reads {label} back with the same imprint",
               theirs.get("digest") == digest,
               f"{theirs.get('digest')} vs {digest}")
            ok(f"...and the same serial for {label}",
               theirs.get("serial") == serial,
               f"{theirs.get('serial')} vs {serial}")

        print("\n[3] Malformed input: never more lenient than OpenSSL\n")
        # The property is one-directional. Refusing something OpenSSL accepts
        # is a choice this project is entitled to make; ACCEPTING something
        # OpenSSL refuses would mean a receipt we call valid and an auditor's
        # tooling calls broken, which is the failure mode that matters.
        bad = (
            ("a truncated token", fix.token(want_digest)[:-6]),
            ("a token with trailing bytes", fix.token(want_digest) + b"\xff\xff"),
            ("indefinite-length BER", b"\x30\x80\x06\x09\x2a\x86\x48\x86"
                                      b"\xf7\x0d\x01\x07\x02\x00\x00"),
            ("a non-minimal length", b"\x30\x81\x02\x05\x00"),
            ("an empty file", b""),
        )
        stricter = []
        for i, (label, blob) in enumerate(bad):
            path = tmp / f"bad-{i}.tsr"
            path.write_bytes(blob)
            theirs_failed = subprocess.run(
                [exe, "ts", "-reply", "-token_in", "-in", path.name, "-text"],
                cwd=tmp, capture_output=True, text=True).returncode != 0
            try:
                tsa.parse(blob)
                ours_failed = False
            except tsa.TSAError:
                ours_failed = True
            if theirs_failed:
                ok(f"{label}: OpenSSL refuses it, so do we", ours_failed,
                   "we accepted what OpenSSL rejected")
            elif ours_failed:
                stricter.append(label)
                ok(f"{label}: we refuse it, OpenSSL does not", True)
            else:
                ok(f"{label}: both accept it", True)
        if stricter:
            print("\n        Deliberate extra strictness, and why it is not a bug:")
            print("        OpenSSL's d2i stops at the end of the outer DER value and")
            print("        ignores whatever follows. A stored `proof` with bytes")
            print("        appended is no longer the receipt the TSA issued, and a")
            print("        receipt is a thing you hand to somebody else byte for byte.")
            for label in stricter:
                print(f"          - {label}")

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  - {f}")
        print("\nA disagreement here means this project is reading timestamp")
        print("receipts differently from the tools an auditor would reach for,")
        print("which is the opposite of what an external anchor is for.")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

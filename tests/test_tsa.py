#!/usr/bin/env python3
"""
RFC 3161 receipts: parsing, binding, and what an anchor is now allowed to claim.

§9.1 says the chain's credibility rests on an external timestamp. Until this
suite existed, `chain.anchor()` accepted any string as the receipt and
`chain.verify()` counted the row as coverage — and two of this project's own
tests passed the literal strings `base64proof` and `base64-token`, which is as
good a demonstration as any that an unchecked field will be filled with
whatever is to hand.

Tokens here are built by `tests/fixtures/rfc3161_fixture.py`, because a receipt
has to commit to a chain head that does not exist until the forecasts do.
That builder is test *input*, not a reference: `tests/interop_rfc3161.py`
requires OpenSSL to read the same fields out of both its tokens and ours.

    python3 tests/test_tsa.py
"""

from __future__ import annotations

import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))

import rfc3161_fixture as fix  # noqa: E402
from spine import chain, ledger, params, timeutil, tsa  # noqa: E402

PASS, FAIL = [], []
T = "2026-06-01T00:00:00.000Z"
HEAD = hashlib.sha256(b"a chain head").hexdigest()


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def refuses(label, fn, exc, fragment):
    try:
        fn()
        ok(label, False, "did not raise")
    except exc as e:
        ok(label, fragment.lower() in str(e).lower(), f"wrong reason: {e}")
    except Exception as e:  # noqa: BLE001
        ok(label, False, f"raised {type(e).__name__} instead: {e}")


def main() -> int:
    print("=" * 76)
    print("RFC 3161 RECEIPTS")
    print("=" * 76)

    print("\n[1] What a well-formed receipt says\n")
    receipt = fix.token(HEAD, gen_time="20260601T000000Z".replace("T", ""),
                        serial=7, policy="1.3.6.1.4.1.13762.3")
    t = tsa.parse(receipt)
    ok("the message imprint is read back exactly", t.hashed_message == HEAD,
       t.hashed_message)
    ok("the digest algorithm is identified", t.hash_algorithm_oid == tsa.SHA256_OID)
    ok("genTime becomes this project's canonical form",
       t.gen_time == "2026-06-01T00:00:00.000Z", t.gen_time)
    ok("...which timeutil itself accepts", timeutil.canonical(t.gen_time) == t.gen_time)
    ok("the serial is read", t.serial == 7, t.serial)
    ok("the policy OID is read", t.policy_oid == "1.3.6.1.4.1.13762.3", t.policy_oid)
    ok("parsing alone claims no binding", t.verified_binding is False)
    ok("and never claims a signature was checked", t.signature_checked is False)
    ok("the summary says so out loud", "signature not checked" in t.summary(),
       t.summary())

    print("\n[2] Binding: does this receipt speak about our hash\n")
    v = tsa.verify(receipt, HEAD)
    ok("a receipt for our head verifies", v.verified_binding is True)
    ok("...and still does not claim a signature", v.signature_checked is False)
    ok("an uppercase digest is the same digest",
       tsa.verify(receipt, HEAD.upper()).verified_binding)
    refuses("a receipt for other data is refused",
            lambda: tsa.verify(fix.token("ff" * 32), HEAD),
            tsa.TSAError, "issued for other data")
    refuses("...and the message names both digests",
            lambda: tsa.verify(fix.token("ff" * 32), HEAD),
            tsa.TSAError, HEAD)
    refuses("a digest that is not 64 hex characters is refused",
            lambda: tsa.verify(receipt, "abc"), tsa.TSAError, "64-character")
    refuses("...and one that is the right length but not hex",
            lambda: tsa.verify(receipt, "z" * 64), tsa.TSAError, "not hex")

    print("\n[3] A receipt over the wrong digest algorithm is not evidence\n")
    sha1 = fix.token(HEAD).replace(
        bytes.fromhex("0609608648016503040201"),   # OID 2.16.840.1.101.3.4.2.1
        bytes.fromhex("06052b0e03021a") + b"\x00\x00\x00\x00")
    refuses("a messageImprint that is not SHA-256 is refused",
            lambda: tsa.verify(sha1, HEAD), tsa.TSAError, "not sha-256")

    print("\n[4] DER, strictly\n")
    refuses("an empty receipt is refused", lambda: tsa.parse(b""),
            tsa.TSAError, "it is empty")
    refuses("trailing bytes after the value are refused",
            lambda: tsa.parse(receipt + b"\x00"), tsa.TSAError, "trailing")
    refuses("a truncated receipt is refused",
            lambda: tsa.parse(receipt[:-5]), tsa.TSAError, "remain")
    # 0x80 as the length byte is BER's indefinite form. Accepting it would mean
    # one byte string with two readings, which is the whole problem with
    # leniency in a format whose job is to be quoted back at you.
    refuses("indefinite-length (BER) encoding is refused",
            lambda: tsa.parse(b"\x30\x80\x05\x00\x00\x00"),
            tsa.TSAError, "ber, not der")
    refuses("a non-minimal length encoding is refused",
            lambda: tsa.parse(b"\x30\x81\x02\x05\x00"),
            tsa.TSAError, "non-minimal")
    refuses("a length field nobody would send is refused",
            lambda: tsa.parse(b"\x30\x85\x01\x01\x01\x01\x01"),
            tsa.TSAError, "plausible")
    deep = b"\x05\x00"
    for _ in range(tsa.MAX_DER_DEPTH + 2):
        deep = fix._tlv(0x30, deep)   # the fixture's length encoder, so the
                                      # nesting is what is under test and not
                                      # a hand-rolled length byte overflowing
    refuses("DER nested past the limit is refused, not crashed",
            lambda: tsa.parse(deep), tsa.TSAError, "nested deeper")

    print("\n[5] Structures that are not a timestamp token\n")
    refuses("a declined response carries nothing to anchor against",
            lambda: tsa.parse(fix.response(HEAD, status=2)),
            tsa.TSAError, "declined")
    ok("a granted response is unwrapped to the same token",
       tsa.parse(fix.response(HEAD)).hashed_message
       == tsa.parse(fix.token(HEAD)).hashed_message)
    not_signed_data = fix._tlv(0x30, fix._oid("1.2.840.113549.1.7.1")
                               + fix._tlv(0x04, b"data"))
    refuses("a SEQUENCE that is not a CMS ContentInfo is refused",
            lambda: tsa.parse(not_signed_data), tsa.TSAError, "not signeddata")
    refuses("a GeneralizedTime with no Z is refused",
            lambda: tsa.parse(fix.token(HEAD, gen_time="20260601000000")),
            tsa.TSAError, "not utc")
    refuses("...and one that is not a real instant",
            lambda: tsa.parse(fix.token(HEAD, gen_time="20260231000000Z")),
            tsa.TSAError, "real instant")
    refuses("...and one with a trailing zero in its fraction, which DER forbids",
            lambda: tsa.parse(fix.token(HEAD, gen_time="20260601000000.500Z")),
            tsa.TSAError, "trailing zero")
    ok("a genuine fractional genTime is kept",
       tsa.parse(fix.token(HEAD, gen_time="20260601000000.25Z")).gen_time
       == "2026-06-01T00:00:00.250Z")

    print("\n[5b] Every structural guard, reached\n")
    # Each of these mutilates one field of a real token. Written because
    # tests/trace_refusals.py reported the guards below as never executed, and
    # a guard nobody has run may be unreachable, inverted, or crash on the way
    # to raising -- all three look identical from outside.
    tlv, oid, integer = fix._tlv, fix._oid, fix._int
    good_tst = fix.tst_info(HEAD)

    def wrap(tst_bytes):
        """A ContentInfo around an arbitrary eContent payload."""
        encap = tlv(0x30, oid(fix.OID_TST_INFO) +
                    tlv(0xA0, tlv(0x04, tst_bytes)))
        sd = tlv(0x30, integer(3) + tlv(0x31, b"") + encap + tlv(0x31, b""))
        return tlv(0x30, oid(fix.OID_SIGNED_DATA) + tlv(0xA0, sd))

    for label, blob, fragment in (
        ("a receipt that is not a SEQUENCE at all", b"\x05\x00",
         "must be a sequence"),
        ("an empty SEQUENCE", tlv(0x30, b""), "empty receipt structure"),
        ("a high-tag-number identifier", b"\x3f\x81\x01\x00",
         "high-tag-number"),
        ("a length field truncated mid-way", b"\x30\x82\x01",
         "inside a length field"),
        ("a length with a leading zero byte",
         b"\x30\x82\x00\x81" + b"\x00" * 0x81, "leading zero byte"),
        ("a ContentInfo with only one element", tlv(0x30, oid("1.2.3")),
         "expected a cms contentinfo"),
        ("an empty OID", tlv(0x30, tlv(0x06, b"") + tlv(0x04, b"")),
         "empty oid"),
        ("an OID ending mid-arc",
         tlv(0x30, tlv(0x06, b"\x2a\x86") + tlv(0x04, b"")), "mid-arc"),
        ("an OID with a non-minimal arc",
         tlv(0x30, tlv(0x06, b"\x2a\x80\x01") + tlv(0x04, b"")),
         "non-minimal oid"),
        ("signedData with no content",
         tlv(0x30, oid(fix.OID_SIGNED_DATA)), "expected a cms contentinfo"),
        ("signedData whose [0] is empty",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) + tlv(0xA0, b"")),
         "signeddata content is missing"),
        ("SignedData that is not a SEQUENCE",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) + tlv(0xA0, tlv(0x04, b"x"))),
         "must be a sequence"),
        ("SignedData with too few fields",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) +
             tlv(0xA0, tlv(0x30, integer(3) + tlv(0x31, b"")))),
         "signeddata is truncated"),
        ("an EncapsulatedContentInfo with one element",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) +
             tlv(0xA0, tlv(0x30, integer(3) + tlv(0x31, b"") +
                           tlv(0x30, oid(fix.OID_TST_INFO)) + tlv(0x31, b"")))),
         "encapsulatedcontentinfo is malformed"),
        ("a CMS object carrying something other than a TSTInfo",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) +
             tlv(0xA0, tlv(0x30, integer(3) + tlv(0x31, b"") +
                           tlv(0x30, oid("1.2.840.113549.1.7.1") +
                               tlv(0xA0, tlv(0x04, b"x"))) + tlv(0x31, b"")))),
         "not a tstinfo"),
        ("a token whose eContent is absent",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) +
             tlv(0xA0, tlv(0x30, integer(3) + tlv(0x31, b"") +
                           tlv(0x30, oid(fix.OID_TST_INFO) + tlv(0xA0, b"")) +
                           tlv(0x31, b"")))),
         "econtent is absent"),
        ("a token whose eContent is not an OCTET STRING",
         tlv(0x30, oid(fix.OID_SIGNED_DATA) +
             tlv(0xA0, tlv(0x30, integer(3) + tlv(0x31, b"") +
                           tlv(0x30, oid(fix.OID_TST_INFO) +
                               tlv(0xA0, tlv(0x05, b""))) + tlv(0x31, b"")))),
         "not an octet string"),
        ("a TSTInfo with too few fields",
         wrap(tlv(0x30, integer(1) + oid("1.2.3"))), "tstinfo is truncated"),
        ("a TSTInfo whose version is not 1",
         wrap(fix.tst_info(HEAD).replace(integer(1), integer(2), 1)),
         "is not the version 1"),
        ("a version field that is not an INTEGER",
         wrap(tlv(0x30, tlv(0x04, b"\x01") + oid("1.2.3") +
                  tlv(0x30, b"") + integer(1) + tlv(0x18, b"20260601000000Z"))),
         "expected an integer"),
        ("an empty INTEGER",
         wrap(tlv(0x30, tlv(0x02, b"") + oid("1.2.3") + tlv(0x30, b"") +
                  integer(1) + tlv(0x18, b"20260601000000Z"))),
         "empty integer"),
        ("a policy field that is not an OID",
         wrap(tlv(0x30, integer(1) + tlv(0x04, b"p") + tlv(0x30, b"") +
                  integer(1) + tlv(0x18, b"20260601000000Z"))),
         "expected an oid"),
        ("a messageImprint that is not a SEQUENCE",
         wrap(tlv(0x30, integer(1) + oid("1.2.3") + tlv(0x04, b"i") +
                  integer(1) + tlv(0x18, b"20260601000000Z"))),
         "messageimprint is malformed"),
        ("a messageImprint hashAlgorithm that is not a SEQUENCE",
         wrap(tlv(0x30, integer(1) + oid("1.2.3") +
                  tlv(0x30, tlv(0x04, b"a") + tlv(0x04, b"d")) +
                  integer(1) + tlv(0x18, b"20260601000000Z"))),
         "hashalgorithm is malformed"),
        ("a hashedMessage that is not an OCTET STRING",
         wrap(tlv(0x30, integer(1) + oid("1.2.3") +
                  tlv(0x30, tlv(0x30, oid(tsa.SHA256_OID)) + tlv(0x05, b"")) +
                  integer(1) + tlv(0x18, b"20260601000000Z"))),
         "hashedmessage is not an octet string"),
        ("a genTime field that is not a GeneralizedTime",
         wrap(tlv(0x30, integer(1) + oid("1.2.3") +
                  tlv(0x30, tlv(0x30, oid(tsa.SHA256_OID)) +
                      tlv(0x04, bytes(32))) + integer(1) + tlv(0x04, b"t"))),
         "expected a generalizedtime"),
        ("a granted status with no token at all",
         tlv(0x30, tlv(0x30, integer(0))), "no token is present"),
        # The three ways a value can simply stop. A constructed value whose
        # body ends between children reaches a different guard from one that
        # ends before its own length byte.
        ("a value that stops before its length byte", b"\x30",
         "before its length byte"),
        ("a body that ends between children", tlv(0x30, b"\x04\x01\x41\x05"),
         "ended before its length byte"),
        ("a body that ends on a tag boundary", tlv(0x30, b"\x04\x01\x41" + b"\x30\x01"),
         "remain"),
        ("a GeneralizedTime that is too short to be one",
         wrap(tlv(0x30, integer(1) + oid("1.2.3") +
                  tlv(0x30, tlv(0x30, oid(tsa.SHA256_OID)) +
                      tlv(0x04, bytes(32))) + integer(1) +
                  tlv(0x18, b"202606Z"))),
         "not yyyymmddhhmmss"),
    ):
        refuses(label, lambda b=blob: tsa.parse(b), tsa.TSAError, fragment)
    ok("...and the good TSTInfo those were derived from still parses",
       tsa.parse(wrap(good_tst)).hashed_message == HEAD)

    print("\n[6] Getting bytes out of a TEXT column\n")
    ok("a hex proof round-trips", tsa.from_hex(receipt.hex()) == receipt)
    ok("...whitespace and newlines included",
       tsa.from_hex(receipt.hex()[:40] + "\n  " + receipt.hex()[40:]) == receipt)
    import base64
    ok("a base64 proof round-trips",
       tsa.from_hex(base64.b64encode(receipt).decode()) == receipt)
    refuses("a proof that is neither is refused",
            lambda: tsa.from_hex("not a receipt!"), tsa.TSAError, "neither hex")
    refuses("an empty proof is refused", lambda: tsa.from_hex("   "),
            tsa.TSAError, "empty")

    print("\n[7] What the chain now counts as coverage\n")
    con = ledger.connect(create=True)
    con.execute(
        """INSERT INTO propositions
           (proposition_hash,statement,resolution_criterion,deadline_utc,
            event_family,horizon_class,created_at)
           VALUES('p1','Will it?','per the source','2026-10-01T00:00:00.000Z',
                  'fam','T1',?)""", (T,))
    con.execute(
        """INSERT INTO reference_class_versions
           (class_name,version,description,n,k,alpha,beta,prior_justification,
            exposure_units,exposure_unit_name,frozen_at,selection_rule_ref)
           VALUES('fam',1,'d',40,4,1.0,9.0,'base rate ~0.10',40.0,'event',?,
                  'rules/fam_v1.md')""", (T,))
    con.commit()

    def register(p_bp):
        return ledger.register_forecast(
            con, proposition_id=1, p_est_bp=p_bp, p_lo_bp=p_bp - 500,
            p_hi_bp=p_bp + 500, uncertainty_method="declared",
            min_width_bp=400, p_base_bp=5000, forecast_kind="independent",
            reference_class_version_id=1, model_version="v1",
            regime_id="r1", created_at="2026-06-02T00:00:00.000Z",
            source="human",
            inputs_manifest_hash=ledger.put_manifest(
                con, "forecast_inputs",
                {"claims": [], "params": params.commit(con, T)}, T))

    register(4000)
    register(4200)
    v = chain.verify(con)
    ok("an unanchored chain reports its whole length as exposed",
       v.ok and v.anchored_through == 0 and v.unanchored_tail == 2, v.summary())

    refuses("anchoring with a receipt for another head is refused",
            lambda: chain.anchor(con, "rfc3161", "tsa://x",
                                 fix.receipt_for("cd" * 32), T),
            chain.ChainError, "does not anchor the current head")
    refuses("...as is a placeholder string, which is what used to be stored",
            lambda: chain.anchor(con, "rfc3161", "tsa://x", "base64proof", T),
            chain.ChainError, "neither hex")

    head = chain.head(con)
    good = fix.receipt_for(head, gen_time=fix.gen_time_from(T))
    refuses("...and a receipt whose genTime disagrees with anchored_at",
            lambda: chain.anchor(con, "rfc3161", "tsa://x", good,
                                 "2026-06-02T00:00:00.000Z"),
            chain.ChainError, "authority on when it was issued")
    ok("a small skew is tolerated, since the caller reads its own clock after",
       chain.anchor(con, "rfc3161", "tsa://x", good,
                    "2026-06-01T00:02:00.000Z") == head)

    v = chain.verify(con)
    ok("a checked anchor is coverage",
       v.ok and v.anchored_through == 2 and v.unanchored_tail == 0, v.summary())
    ok("...and is reported as checked", v.verified_anchors == 1, v.verified_anchors)
    ok("nothing is reported as unverifiable yet", v.unverifiable_anchors == 0)

    register(4400)
    v = chain.verify(con)
    ok("records after the anchor are flagged, not covered by it",
       v.unanchored_tail == 1, v.summary())

    print("\n[8] Anchors this project cannot check offline\n")
    con.execute(
        "INSERT INTO chain_anchors(chain_head_hash, method, external_ref, proof, "
        "anchored_at) VALUES(?,?,?,?,?)",
        (chain.head(con), "public_append_only_log", "log://entry/9", "opaque", T))
    con.commit()
    v = chain.verify(con)
    ok("a public-log anchor is recorded", v.unverifiable_anchors == 1,
       v.unverifiable_anchors)
    ok("...but moves coverage not at all", v.unanchored_tail == 1, v.summary())
    ok("...and the summary says why", "not checkable offline" in v.summary(),
       v.summary())

    # A row written straight into the table, bypassing anchor()'s check. This is
    # how a restored backup or a migration would arrive, and it must not quietly
    # become coverage.
    con.execute(
        "INSERT INTO chain_anchors(chain_head_hash, method, external_ref, proof, "
        "anchored_at) VALUES(?,?,?,?,?)",
        (chain.head(con), "rfc3161", "tsa://forged",
         fix.receipt_for("ee" * 32), T))
    con.commit()
    v = chain.verify(con)
    ok("a receipt inserted behind anchor()'s back is not coverage either",
       v.unanchored_tail == 1 and v.unverifiable_anchors == 2, v.summary())
    ok("the chain itself is still intact, which is a separate question", v.ok)

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 76)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

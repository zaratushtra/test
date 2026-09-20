"""
Build RFC 3161 timestamp tokens, as test *input* — not as a reference.

`tests/interop_rfc3161.py` checks `spine/tsa.py` against OpenSSL, which is the
authority. This module exists for a different job: the ordinary suites need a
receipt committing to a chain head they only learn at run time, and a head is
whatever the forecasts hashed to. A committed fixture cannot commit to a hash
that does not exist yet.

So tokens are built here. **This is deliberately not a second opinion about the
spec**: if it and `spine/tsa.py` shared a misunderstanding, both would agree and
both would be wrong. That risk is retired elsewhere — the interop check feeds
tokens from this module to `openssl ts -reply -text` and requires OpenSSL to
read back the same fields. This module is trusted only as far as that check
reaches.

The tokens carry no signature. Nothing in `spine/tsa.py` verifies one (see its
docstring for why, and for what closing that would take), so a signed fixture
would imply a check that does not happen.
"""

from __future__ import annotations

import hashlib

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
DEFAULT_POLICY = "1.3.6.1.4.1.13762.3"


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    out = bytes([parts[0] * 40 + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append((p & 0x7F) | 0x80)
            p >>= 7
        out += bytes(reversed(chunk))
    return _tlv(0x06, out)


def _int(v: int) -> bytes:
    if v == 0:
        return _tlv(0x02, b"\x00")
    n = (v.bit_length() + 8) // 8
    return _tlv(0x02, v.to_bytes(n, "big", signed=True))


def tst_info(digest_hex: str, gen_time: str = "20260920143057Z",
             serial: int = 1, policy: str = DEFAULT_POLICY) -> bytes:
    """TSTInfo over a hex digest. `gen_time` is a DER GeneralizedTime."""
    imprint = _tlv(0x30,
                   _tlv(0x30, _oid(OID_SHA256) + _tlv(0x05, b"")) +
                   _tlv(0x04, bytes.fromhex(digest_hex)))
    return _tlv(0x30,
                _int(1) + _oid(policy) + imprint + _int(serial) +
                _tlv(0x18, gen_time.encode("ascii")))


def token(digest_hex: str, **kw) -> bytes:
    """A TimeStampToken (CMS ContentInfo) carrying that TSTInfo."""
    encap = _tlv(0x30, _oid(OID_TST_INFO) +
                 _tlv(0xA0, _tlv(0x04, tst_info(digest_hex, **kw))))
    signed_data = _tlv(0x30,
                       _int(3) +            # CMSVersion
                       _tlv(0x31, b"") +    # digestAlgorithms
                       encap +
                       _tlv(0x31, b""))     # signerInfos
    return _tlv(0x30, _oid(OID_SIGNED_DATA) + _tlv(0xA0, signed_data))


def response(digest_hex: str, status: int = 0, **kw) -> bytes:
    """A full TimeStampResp: PKIStatusInfo plus the token."""
    body = _tlv(0x30, _int(status))
    if status in (0, 1):
        body += token(digest_hex, **kw)
    return _tlv(0x30, body)


def receipt_for(head_hash: str, **kw) -> str:
    """A hex-encoded receipt for a chain head, ready for `chain_anchors.proof`."""
    return token(head_hash, **kw).hex()


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def gen_time_from(canonical_iso: str) -> str:
    """
    This project's `YYYY-MM-DDTHH:MM:SS.sssZ` as a DER GeneralizedTime.

    Separate because doing it inline with `.replace()` is easy to get subtly
    wrong — the first attempt left the `T` in place and produced a 14-character
    string that looked right and was not.
    """
    d, t = canonical_iso.rstrip("Z").split("T")
    return d.replace("-", "") + t.split(".")[0].replace(":", "") + "Z"

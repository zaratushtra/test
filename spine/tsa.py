"""
RFC 3161 timestamp receipts: parsing one, and checking it commits to our head.

§9.1 is the load-bearing claim of this whole design. A hash chain proves
internal consistency; it does nothing about fabricating the entire chain later
with backdated timestamps. Pre-registration only means something if an
independent party timestamped the head, so `chain_anchors` exists and
`chain.anchor()` records the receipt.

**And nothing looked at the receipt.** `anchor()` took `proof` as an opaque
string and stored it. `chain.verify()` then counted forecasts as anchored from
the presence of a row. A receipt for somebody else's data, a receipt re-used
from last month's head, or the literal text `trust me` all produced the same
report — while the function's own docstring said "a self-signed value here would
prove nothing, which is the whole point". The discipline was stated in prose and
enforced nowhere, which is the same failure §12's sequential testing had.

This module closes the part that can be closed offline: **does this token's
messageImprint actually commit to the hash we are claiming it anchors?** That is
the question a re-used or mismatched receipt fails, and it needs no network, no
clock and no trust.

What this module does NOT do
----------------------------
It does not verify the TSA's signature, and it does not validate any
certificate chain. It therefore does not establish that a *trustworthy* party
issued the token — only that the token in hand is well-formed DER and speaks
about our hash. Anyone can mint a token with the structure below.

That is a real limitation and it is stated here rather than glossed, because
overstating what a check establishes is the specific error this module was
written to correct. Closing it needs: the signature over `signedAttrs` verified
with the embedded certificate, that certificate pinned by fingerprint or chained
to a root this project declares, and the `messageDigest` signed attribute
checked against the eContent. `verified_binding` in the returned record names
exactly what was and was not established, so a caller cannot mistake one for the
other.

The parser
----------
Stdlib only, and strict about DER rather than lenient about BER: indefinite
lengths, non-minimal length encodings and trailing bytes are all refused. A
lenient parser is a liability here — two parsers that disagree about what a byte
string means is precisely how a receipt gets read one way by an auditor and
another way by us.
"""

from __future__ import annotations

import binascii
from dataclasses import dataclass
from datetime import datetime, timezone

from . import timeutil

# Nesting depth the DER reader will descend. A timestamp token is about eight
# levels deep at its deepest; a hundred is far more than any real structure and
# still refuses long before the interpreter stack does. Same reasoning as
# canonical.MAX_DEPTH, and learned the same way.
MAX_DER_DEPTH = 100

# The only digest this project will accept in a messageImprint. A receipt over
# a SHA-1 imprint is not evidence about a SHA-256 chain head, and accepting a
# weaker algorithm here would let a collision stand in for the head.
SHA256_OID = "2.16.840.1.101.3.4.2.1"

OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_TST_INFO = "1.2.840.113549.1.9.16.1.4"

# PKIStatus values that mean a token was issued. Anything else is the TSA
# declining, and a declined response carries no token to check.
GRANTED = {0: "granted", 1: "granted with modifications"}


class TSAError(ValueError):
    """A timestamp receipt could not be parsed, or does not say what is claimed."""


# ---------------------------------------------------------------------------
# a strict DER reader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Node:
    tag: int
    constructed: bool
    body: bytes          # contents, excluding tag and length
    children: tuple      # parsed children when constructed, else ()

    @property
    def cls(self) -> int:
        return self.tag & 0xC0

    @property
    def number(self) -> int:
        return self.tag & 0x1F


def _read_one(data: bytes, i: int, depth: int) -> tuple[Node, int]:
    if depth > MAX_DER_DEPTH:
        raise TSAError(f"DER nested deeper than {MAX_DER_DEPTH} levels")
    if i >= len(data):
        # Also the empty-input case: `parse_der` used to check for that
        # separately, which left this branch unreachable by any caller. A guard
        # nothing can reach is a guard nobody has checked, so the two were
        # merged rather than one of them being kept for tidiness.
        raise TSAError(
            "the receipt ends where a value should start: it is empty, or it "
            "was cut short")
    tag = data[i]
    i += 1
    if tag & 0x1F == 0x1F:
        raise TSAError("high-tag-number form is not used in these structures")
    if i >= len(data):
        raise TSAError("DER ended before its length byte")
    n = data[i]
    i += 1
    if n == 0x80:
        raise TSAError(
            "indefinite-length encoding: this is BER, not DER. A timestamp "
            "receipt must have exactly one byte-for-byte encoding, or two "
            "readers can disagree about what it says")
    if n & 0x80:
        k = n & 0x7F
        if k > 4:
            raise TSAError(f"length field of {k} bytes is not a plausible receipt")
        if i + k > len(data):
            raise TSAError("DER ended inside a length field")
        length = int.from_bytes(data[i:i + k], "big")
        if length < 0x80:
            raise TSAError(
                f"non-minimal length encoding ({length} in {k} bytes): DER "
                "requires the shortest form")
        if k > 1 and data[i] == 0:
            raise TSAError("non-minimal length encoding: leading zero byte")
        i += k
    else:
        length = n
    if i + length > len(data):
        raise TSAError(
            f"value claims {length} bytes but only {len(data) - i} remain")
    body = data[i:i + length]
    i += length
    constructed = bool(tag & 0x20)
    children = tuple(_read_all(body, depth + 1)) if constructed else ()
    return Node(tag, constructed, body, children), i


def _read_all(data: bytes, depth: int = 0) -> list[Node]:
    out, i = [], 0
    while i < len(data):
        node, i = _read_one(data, i, depth)
        out.append(node)
    return out


def parse_der(data: bytes) -> Node:
    """One complete DER value, with nothing after it."""
    node, i = _read_one(data, 0, 0)
    if i != len(data):
        raise TSAError(
            f"{len(data) - i} trailing byte(s) after the DER value. A receipt "
            "with something appended is not the receipt that was issued")
    return node


def _oid(node: Node) -> str:
    """An OBJECT IDENTIFIER's dotted form."""
    if node.tag != 0x06:
        raise TSAError(f"expected an OID, got tag 0x{node.tag:02x}")
    b = node.body
    if not b:
        raise TSAError("empty OID")
    first = b[0]
    parts = [str(first // 40), str(first % 40)]
    value, started = 0, False
    for byte in b[1:]:
        if not started and byte == 0x80:
            raise TSAError("non-minimal OID arc encoding")
        started = True
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            parts.append(str(value))
            value, started = 0, False
    if started:
        raise TSAError("OID ends mid-arc")
    return ".".join(parts)


def _int(node: Node) -> int:
    if node.tag != 0x02:
        raise TSAError(f"expected an INTEGER, got tag 0x{node.tag:02x}")
    if not node.body:
        raise TSAError("empty INTEGER")
    return int.from_bytes(node.body, "big", signed=True)


def _generalized_time(node: Node) -> str:
    """
    A GeneralizedTime as this project's canonical timestamp.

    DER requires UTC with a trailing `Z` and no zone offset, so anything else
    here is a malformed receipt rather than a formatting preference.
    """
    if node.tag != 0x18:
        raise TSAError(f"expected a GeneralizedTime, got tag 0x{node.tag:02x}")
    text = node.body.decode("ascii", errors="replace")
    if not text.endswith("Z"):
        raise TSAError(f"GeneralizedTime {text!r} is not UTC; DER requires a 'Z'")
    stem = text[:-1]
    frac = ""
    if "." in stem:
        stem, frac = stem.split(".", 1)
        if not frac or frac.rstrip("0") != frac:
            raise TSAError(
                f"GeneralizedTime {text!r} has a trailing zero in its fraction; "
                "DER forbids it")
    if len(stem) != 14 or not stem.isdigit():
        raise TSAError(f"GeneralizedTime {text!r} is not YYYYMMDDHHMMSS")
    try:
        dt = datetime.strptime(stem, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise TSAError(f"GeneralizedTime {text!r} is not a real instant") from e
    if frac:
        dt = dt.replace(microsecond=int((frac + "000000")[:6]))
    return timeutil.iso(dt)


# ---------------------------------------------------------------------------
# the structures
# ---------------------------------------------------------------------------

def _unwrap_to_tst_info(root: Node) -> Node:
    """
    Descend from whatever we were handed to the TSTInfo SEQUENCE.

    Accepts either a full `TimeStampResp` (status plus token, what a TSA
    returns) or a bare `TimeStampToken` (the CMS ContentInfo alone, which is
    what most tools store). Both turn up in the wild and refusing one of them
    would mean the check gets skipped rather than fixed.
    """
    if root.tag != 0x30:
        raise TSAError("a receipt must be a SEQUENCE")
    kids = root.children
    if not kids:
        raise TSAError("empty receipt structure")

    # TimeStampResp starts with PKIStatusInfo, itself a SEQUENCE beginning with
    # an INTEGER. ContentInfo starts with an OID. That tells them apart.
    if kids[0].tag == 0x30 and kids[0].children and kids[0].children[0].tag == 0x02:
        status = _int(kids[0].children[0])
        if status not in GRANTED:
            raise TSAError(
                f"the TSA declined to issue a token (PKIStatus {status}). "
                "There is nothing here to anchor against")
        if len(kids) < 2:
            raise TSAError(
                f"status says {GRANTED[status]} but no token is present")
        content_info = kids[1]
    else:
        content_info = root

    if content_info.tag != 0x30 or len(content_info.children) < 2:
        raise TSAError("expected a CMS ContentInfo")
    if _oid(content_info.children[0]) != OID_SIGNED_DATA:
        raise TSAError("ContentInfo is not signedData")
    explicit = content_info.children[1]
    if not explicit.constructed or not explicit.children:
        raise TSAError("signedData content is missing")
    signed_data = explicit.children[0]
    if signed_data.tag != 0x30:
        raise TSAError("SignedData must be a SEQUENCE")

    # SignedData ::= version, digestAlgorithms, encapContentInfo, ...
    if len(signed_data.children) < 3:
        raise TSAError("SignedData is truncated")
    encap = signed_data.children[2]
    if encap.tag != 0x30 or len(encap.children) < 2:
        raise TSAError("EncapsulatedContentInfo is malformed")
    if _oid(encap.children[0]) != OID_TST_INFO:
        raise TSAError(
            "the signed content is not a TSTInfo; this is a CMS object but not "
            "a timestamp token")
    holder = encap.children[1]
    if not holder.constructed or not holder.children:
        raise TSAError("eContent is absent: this token does not carry its TSTInfo")
    octets = holder.children[0]
    if octets.tag != 0x04:
        raise TSAError("eContent is not an OCTET STRING")
    return parse_der(octets.body)


@dataclass(frozen=True)
class Timestamp:
    """What a receipt asserts, and what checking it established."""
    hash_algorithm_oid: str
    hashed_message: str      # lowercase hex
    gen_time: str            # canonical YYYY-MM-DDTHH:MM:SS.sssZ
    serial: int
    policy_oid: str
    verified_binding: bool   # imprint matches the hash we claimed
    signature_checked: bool  # always False; see the module docstring

    def summary(self) -> str:
        return (f"{self.gen_time} serial {self.serial} over "
                f"{self.hashed_message[:16]}... (signature not checked)")


def parse(receipt: bytes) -> Timestamp:
    """
    Read a receipt without checking what it commits to.

    Returns what the token says. `verified_binding` is False because nothing was
    compared against it — use `verify()` when there is a hash to check.
    """
    tst = _unwrap_to_tst_info(parse_der(receipt))
    kids = tst.children
    if len(kids) < 5:
        raise TSAError("TSTInfo is truncated")
    version = _int(kids[0])
    if version != 1:
        raise TSAError(f"TSTInfo version {version} is not the version 1 RFC 3161 defines")
    policy = _oid(kids[1])
    imprint = kids[2]
    if imprint.tag != 0x30 or len(imprint.children) < 2:
        raise TSAError("messageImprint is malformed")
    alg = imprint.children[0]
    if alg.tag != 0x30 or not alg.children:
        raise TSAError("messageImprint hashAlgorithm is malformed")
    alg_oid = _oid(alg.children[0])
    digest_node = imprint.children[1]
    if digest_node.tag != 0x04:
        raise TSAError("hashedMessage is not an OCTET STRING")
    return Timestamp(
        hash_algorithm_oid=alg_oid,
        hashed_message=binascii.hexlify(digest_node.body).decode("ascii"),
        gen_time=_generalized_time(kids[4]),
        serial=_int(kids[3]),
        policy_oid=policy,
        verified_binding=False,
        signature_checked=False,
    )


def verify(receipt: bytes, expected_hash: str) -> Timestamp:
    """
    Check that a receipt commits to `expected_hash`, or refuse.

    `expected_hash` is lowercase hex — for this project, a chain head. The
    comparison is on the digest itself, so a receipt issued for a different
    head, or re-used from an earlier one, fails here rather than being counted
    as coverage.

    Returns a `Timestamp` with `verified_binding=True` and, still,
    `signature_checked=False`. See the module docstring for why that distinction
    is kept explicit rather than collapsed into a single boolean.
    """
    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
        raise TSAError(f"expected a 64-character hex digest, got {expected_hash!r}")
    want = expected_hash.lower()
    try:
        binascii.unhexlify(want)
    except (binascii.Error, ValueError) as e:
        raise TSAError(f"{expected_hash!r} is not hex") from e

    ts = parse(receipt)
    if ts.hash_algorithm_oid != SHA256_OID:
        raise TSAError(
            f"messageImprint uses {ts.hash_algorithm_oid}, not SHA-256. A "
            "receipt over a different digest is not evidence about this chain")
    if ts.hashed_message != want:
        raise TSAError(
            f"this receipt commits to {ts.hashed_message}, not {want}. It was "
            "issued for other data, or re-used from an earlier head")
    return Timestamp(
        hash_algorithm_oid=ts.hash_algorithm_oid,
        hashed_message=ts.hashed_message,
        gen_time=ts.gen_time,
        serial=ts.serial,
        policy_oid=ts.policy_oid,
        verified_binding=True,
        signature_checked=False,
    )


def from_hex(text: str) -> bytes:
    """
    A receipt stored as text, back to bytes.

    `chain_anchors.proof` is a TEXT column, so a DER receipt lives there
    hex-encoded or base64. Both are accepted; anything else is refused rather
    than guessed at.
    """
    stripped = "".join(text.split())
    if not stripped:
        raise TSAError("empty proof")
    try:
        return binascii.unhexlify(stripped)
    except (binascii.Error, ValueError):
        pass
    import base64
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError) as e:
        raise TSAError(
            "proof is neither hex nor base64. A receipt has to be stored in a "
            "form that round-trips to the bytes the TSA signed") from e

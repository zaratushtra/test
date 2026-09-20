"""
Forecast hash chain: append, verify, anchor.

v2 §9.1 is explicit that a hash chain proves internal consistency and detects
tampering *within* a chain, but does not establish when the chain was created —
nothing stops someone fabricating a whole chain later with backdated timestamps.
Pre-registration is this system's core credibility claim, so `anchor()` exists and
`verify()` reports anchor coverage rather than pretending the chain alone suffices.

**And the receipt is now read rather than filed.** `anchor()` used to take
`proof` as an opaque string and store it, while its own docstring said "a
self-signed value here would prove nothing, which is the whole point" — so a
receipt issued for other data, one re-used from last month's head, or the
literal text `base64proof` (which is what two of this project's own tests
passed) all produced the same report of coverage. An RFC 3161 receipt is now
parsed and required to commit to the head it claims to anchor, and coverage is
counted from anchors that were checked. See `spine/tsa.py`, including the part
it explicitly does not establish.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import timeutil
from . import tsa
from .canonical import GENESIS_HASH, chain_hash

# How far an anchor's claimed `anchored_at` may sit from the genTime the TSA
# itself asserts. The receipt is the authority; this tolerance exists only so a
# caller reading its own clock a moment later is not refused. A larger gap means
# the caller is describing a different event from the one the receipt records.
MAX_ANCHOR_TIME_SKEW_SECONDS = 300.0

# Methods whose proof this project can check without a network. Anything else
# is stored and counted separately, never silently as if it had been verified.
VERIFIABLE_METHODS = ("rfc3161",)

# The exact fields a forecast commits to. Order is irrelevant (keys are sorted at
# canonicalisation) but membership is not: anything omitted here can be changed
# after the fact without breaking verification.
COMMITTED_FIELDS = (
    "proposition_id",
    "contract_id",
    "p_est_bp",
    "p_lo_bp",
    "p_hi_bp",
    "uncertainty_method",
    "min_width_bp",
    "p_base_bp",
    "p_market_bp",
    "forecast_kind",
    "reference_class_version_id",
    "inputs_manifest_hash",
    "model_version",
    "regime_id",
    "created_at",
    "source",
    "source_detail",
)


class ChainError(RuntimeError):
    """The chain is broken, or an operation would break it."""


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    length: int
    first_break: int | None
    reason: str | None
    anchored_through: int
    unanchored_tail: int
    # Anchors whose receipt was read and found to commit to the head it names.
    verified_anchors: int = 0
    # Anchors this project cannot check offline -- a public-log or blockchain
    # method, or an RFC 3161 receipt that no longer parses. Reported rather
    # than counted, because "recorded" and "checked" are different claims.
    unverifiable_anchors: int = 0

    def summary(self) -> str:
        if not self.ok:
            return f"BROKEN at row {self.first_break}: {self.reason}"
        if self.length == 0:
            return "empty chain"
        tail = ""
        if self.unanchored_tail:
            tail = (f"; {self.unanchored_tail} record(s) past the last anchor are "
                    "tamper-evident but not time-proven")
        unchecked = ""
        if self.unverifiable_anchors:
            unchecked = (f"; {self.unverifiable_anchors} anchor(s) recorded but "
                         "not checkable offline, and not counted as coverage")
        return (f"intact, {self.length} record(s), anchored through "
                f"{self.anchored_through}{tail}{unchecked}")


def committed_payload(row: dict) -> dict:
    """Extract exactly the fields the hash covers."""
    return {k: row.get(k) for k in COMMITTED_FIELDS}


def head(con: sqlite3.Connection) -> str:
    """Current chain head, or the genesis sentinel when empty."""
    r = con.execute(
        "SELECT forecast_hash FROM forecasts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return r[0] if r else GENESIS_HASH


def compute_hash(row: dict, prev: str) -> str:
    return chain_hash(committed_payload(row), prev)


def verify(con: sqlite3.Connection) -> VerificationResult:
    """
    Recompute every link. Detects edited fields, reordered rows and forged
    prev_hash values.
    """
    rows = con.execute(
        f"SELECT id, forecast_hash, prev_hash, {', '.join(COMMITTED_FIELDS)} "
        "FROM forecasts ORDER BY id ASC"
    ).fetchall()
    cols = ["id", "forecast_hash", "prev_hash", *COMMITTED_FIELDS]

    expected_prev = GENESIS_HASH
    for i, raw in enumerate(rows, start=1):
        row = dict(zip(cols, raw))
        if row["prev_hash"] != expected_prev:
            return VerificationResult(
                False, len(rows), i,
                f"prev_hash {row['prev_hash'][:12]}... != expected {expected_prev[:12]}...",
                0, 0,
            )
        recomputed = compute_hash(row, expected_prev)
        if recomputed != row["forecast_hash"]:
            return VerificationResult(
                False, len(rows), i,
                f"content hash mismatch: stored {row['forecast_hash'][:12]}..., "
                f"recomputed {recomputed[:12]}...",
                0, 0,
            )
        expected_prev = row["forecast_hash"]

    anchored_through, unanchored, verified, unverifiable = _anchor_coverage(
        con, rows, cols)
    return VerificationResult(True, len(rows), None, None, anchored_through,
                              unanchored, verified, unverifiable)


def _anchor_coverage(con, rows, cols) -> tuple[int, int, int, int]:
    """
    How far along the chain a *checked* external anchor exists.

    Coverage used to be the presence of a row. That made the report a
    restatement of what somebody had typed: `chain_anchors` is an ordinary
    table, and a row asserting the current head had exactly the same effect
    whether or not any timestamp authority had ever seen it.

    So a row now earns coverage by carrying a receipt that parses and commits
    to the head it names. A row whose method this project cannot check offline
    is counted in `unverifiable_anchors` and moves coverage not at all — it
    remains on the record, since discarding a real public-log anchor because we
    lack a checker would be worse, but it stops being evidence by default.
    """
    verified: set[str] = set()
    unverifiable = 0
    for head_hash, method, proof in con.execute(
            "SELECT chain_head_hash, method, proof FROM chain_anchors").fetchall():
        if method not in VERIFIABLE_METHODS:
            unverifiable += 1
            continue
        try:
            tsa.verify(tsa.from_hex(proof), head_hash)
        except tsa.TSAError:
            # Refused at insert, so this arrived by another route -- a direct
            # write, a restored backup, a schema migration. Not fatal to the
            # chain, which is a separate question, but not coverage either.
            unverifiable += 1
            continue
        verified.add(head_hash)

    if not verified:
        return 0, len(rows), 0, unverifiable
    last = 0
    for i, raw in enumerate(rows, start=1):
        if dict(zip(cols, raw))["forecast_hash"] in verified:
            last = i
    return last, len(rows) - last, len(verified), unverifiable


def anchor(
    con: sqlite3.Connection,
    method: str,
    external_ref: str,
    proof: str,
    anchored_at: str,
) -> str:
    """
    Record an external timestamp over the current chain head.

    Obtaining the receipt is out of scope and must come from something we do not
    control — an RFC 3161 TSA, or a public append-only log. A self-signed value
    here would prove nothing, which is the whole point.

    For `rfc3161` the receipt is **read before it is stored**: it must parse, it
    must use SHA-256, and its messageImprint must be the head being anchored.
    A receipt issued for other data or re-used from an earlier head is refused
    at the point it would enter the record, rather than sitting there being
    counted as coverage. `anchored_at` must also agree with the TSA's own
    genTime, since the receipt is the authority on when it was issued and the
    caller's clock is not.

    What this does not establish is who signed the token; `spine/tsa.py` says
    so at length, and `VerificationResult.signature_checked` stays False.
    """
    h = head(con)
    if h == GENESIS_HASH:
        raise ChainError("nothing to anchor: chain is empty")

    if method in VERIFIABLE_METHODS:
        try:
            ts = tsa.verify(tsa.from_hex(proof), h)
        except tsa.TSAError as e:
            raise ChainError(
                f"this {method} receipt does not anchor the current head: {e}"
            ) from e
        skew = abs(timeutil.parse(anchored_at).timestamp()
                   - timeutil.parse(ts.gen_time).timestamp())
        if skew > MAX_ANCHOR_TIME_SKEW_SECONDS:
            raise ChainError(
                f"anchored_at {anchored_at} is {skew:.0f}s from the receipt's "
                f"own genTime {ts.gen_time}, past the "
                f"{MAX_ANCHOR_TIME_SKEW_SECONDS:.0f}s tolerance. The receipt is "
                "the authority on when it was issued")
    con.execute(
        "INSERT INTO chain_anchors(chain_head_hash, method, external_ref, proof, "
        "anchored_at) VALUES(?,?,?,?,?)",
        (h, method, external_ref, proof, anchored_at),
    )
    con.commit()
    return h

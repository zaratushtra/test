"""
Forecast hash chain: append, verify, anchor.

v2 §9.1 is explicit that a hash chain proves internal consistency and detects
tampering *within* a chain, but does not establish when the chain was created —
nothing stops someone fabricating a whole chain later with backdated timestamps.
Pre-registration is this system's core credibility claim, so `anchor()` exists and
`verify()` reports anchor coverage rather than pretending the chain alone suffices.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .canonical import GENESIS_HASH, chain_hash

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

    def summary(self) -> str:
        if not self.ok:
            return f"BROKEN at row {self.first_break}: {self.reason}"
        if self.length == 0:
            return "empty chain"
        tail = ""
        if self.unanchored_tail:
            tail = (f"; {self.unanchored_tail} record(s) past the last anchor are "
                    "tamper-evident but not time-proven")
        return f"intact, {self.length} record(s), anchored through {self.anchored_through}{tail}"


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

    anchored_through, unanchored = _anchor_coverage(con, rows, cols)
    return VerificationResult(True, len(rows), None, None, anchored_through, unanchored)


def _anchor_coverage(con, rows, cols) -> tuple[int, int]:
    """How far along the chain an external anchor exists."""
    anchors = {
        r[0] for r in con.execute("SELECT chain_head_hash FROM chain_anchors").fetchall()
    }
    if not anchors:
        return 0, len(rows)
    last = 0
    for i, raw in enumerate(rows, start=1):
        if dict(zip(cols, raw))["forecast_hash"] in anchors:
            last = i
    return last, len(rows) - last


def anchor(
    con: sqlite3.Connection,
    method: str,
    external_ref: str,
    proof: str,
    anchored_at: str,
) -> str:
    """
    Record an external timestamp over the current chain head.

    This function stores the receipt; obtaining it is out of scope and must come
    from something we do not control — an RFC 3161 TSA, or a public append-only
    log. A self-signed value here would prove nothing, which is the whole point.
    """
    h = head(con)
    if h == GENESIS_HASH:
        raise ChainError("nothing to anchor: chain is empty")
    con.execute(
        "INSERT INTO chain_anchors(chain_head_hash, method, external_ref, proof, "
        "anchored_at) VALUES(?,?,?,?,?)",
        (h, method, external_ref, proof, anchored_at),
    )
    con.commit()
    return h

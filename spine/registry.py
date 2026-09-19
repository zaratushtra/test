"""
Contract registry: screened markets into the database.

The missing join between `phase0/screen_markets.py`, which reads the venue, and
`db/schema_v2.sql`, which is where forecasts get bound to something specific.

v2 §10.1 is the reason this is not a thin insert. A contract is not "a market" —
it is an exact outcome token under an exact rules version with an exact deadline
and a named resolution source, and the *rules* govern settlement, not the title.
So:

  * The **rules text is hashed** and that hash is part of the contract's identity.
    When a venue amends resolution text — which Polymarket does post-listing — the
    amended version is a *new* contract row, not an update to the old one. A
    standing forecast therefore keeps pointing at the text it was made against,
    and the divergence becomes visible instead of silently rewriting history.
  * **Payout states are recorded, not assumed binary.** A UMA "Unknown"
    resolution pays 0.50, so a contract that only knows about YES and NO cannot
    represent what actually happened to the position.
  * **Eligibility is stamped with a time.** It is a fact about a jurisdiction on a
    date, not a permanent property of the market.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .canonical import content_hash
from . import timeutil

# One timestamp format for the whole project. spine/timeutil.py has the
# lexicographic-ordering bug that made this non-negotiable; six copies of
# these helpers used to live in six modules and disagreed on whole seconds.
_now = timeutil.now
_iso = timeutil.iso
_parse = timeutil.parse
_canon = timeutil.canonical

# Binary Polymarket markets. Recorded explicitly because the third state is the
# one that breaks naive P&L accounting.
BINARY_PAYOUTS = {"YES": 1.0, "NO": 0.0, "UNKNOWN": 0.5}

# Jurisdictions where new positions cannot be opened. Verified 19 Sep 2026; this
# is a snapshot of a venue policy, not an authority.
CLOSE_ONLY = {"GB", "UK"}


class RegistryError(RuntimeError):
    """A market could not be registered as a contract."""


@dataclass(frozen=True)
class IngestReport:
    seen: int
    inserted: int
    already_present: int
    skipped: list[tuple[str, str]]   # (market_id, reason)

    def summary(self) -> str:
        return (f"{self.seen} screened, {self.inserted} new, "
                f"{self.already_present} already registered, {len(self.skipped)} skipped")


def eligibility_for(jurisdiction: str | None) -> str:
    """Map a jurisdiction to a contract eligibility status."""
    if not jurisdiction or jurisdiction.lower() == "unknown":
        return "unknown"
    return "close_only" if jurisdiction.upper() in CLOSE_ONLY else "tradeable"




def rules_version_hash(rules_text: str) -> str:
    """Identity of a rules version. Amended text is a different contract."""
    return content_hash({"rules_text": rules_text})


def ingest_markets(
    con: sqlite3.Connection,
    markets: list[dict],
    jurisdiction: str | None,
    venue: str = "polymarket",
    now: str | None = None,
) -> IngestReport:
    """
    Register screened markets as contracts.

    `markets` are rows as produced by the Phase 0 screen: at minimum `id`,
    `question`, `end_date`. A market missing a deadline or resolution text is
    *skipped with a reason* rather than registered with a placeholder — a
    contract whose settlement rule is unknown cannot be forecast against, and
    inventing one would be worse than having none.
    """
    ts = _canon(now) if now else _now()
    elig = eligibility_for(jurisdiction)
    inserted = present = 0
    skipped: list[tuple[str, str]] = []

    for m in markets:
        mid = str(m.get("id") or "")
        if not mid:
            skipped.append(("<no id>", "market has no identifier"))
            continue

        deadline = m.get("end_date") or m.get("endDate")
        if not deadline:
            skipped.append((mid, "no deadline: cannot bind a resolution time"))
            continue

        rules = (m.get("rules_text") or m.get("description") or "").strip()
        if not rules:
            skipped.append((mid, "no resolution text: rules govern settlement, not the title"))
            continue

        token = str(m.get("outcome_token_id") or m.get("clob_token_id") or f"{mid}:YES")
        rvh = rules_version_hash(rules)

        exists = con.execute(
            "SELECT 1 FROM contracts WHERE venue=? AND market_id=? AND "
            "outcome_token_id=? AND rules_version_hash=?",
            (venue, mid, token, rvh),
        ).fetchone()
        if exists:
            present += 1
            continue

        con.execute(
            """INSERT INTO contracts
               (venue, market_id, outcome_token_id, rules_text, rules_version_hash,
                deadline_utc, resolution_source, payout_states, fee_schedule_ref,
                eligibility_status, eligibility_checked_at, first_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (venue, mid, token, rules, rvh, str(deadline),
             str(m.get("resolution_source") or "venue_rules_text"),
             json.dumps(BINARY_PAYOUTS, sort_keys=True),
             m.get("fee_schedule_ref"),
             elig, ts, ts),
        )
        inserted += 1

    con.commit()
    return IngestReport(len(markets), inserted, present, skipped)


def ensure_proposition(
    con: sqlite3.Connection,
    statement: str,
    resolution_criterion: str,
    deadline_utc: str,
    event_family: str,
    horizon_class: str,
    now: str | None = None,
) -> int:
    """
    Register a research proposition, returning its id.

    Kept separate from the contract on purpose (§10.1): the proposition is what
    we are actually trying to know, the contract is an instrument that may or may
    not measure it. Binding them is a reviewable act, not an identity.
    """
    if horizon_class not in ("T1", "T2", "T3"):
        raise RegistryError(f"horizon_class must be T1/T2/T3, got {horizon_class!r}")
    ts = _canon(now) if now else _now()
    phash = content_hash({
        "statement": statement,
        "resolution_criterion": resolution_criterion,
        "deadline_utc": deadline_utc,
    })
    row = con.execute(
        "SELECT id FROM propositions WHERE proposition_hash=?", (phash,)
    ).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        """INSERT INTO propositions
           (proposition_hash, statement, resolution_criterion, deadline_utc,
            event_family, horizon_class, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (phash, statement, resolution_criterion, deadline_utc,
         event_family, horizon_class, ts),
    )
    con.commit()
    return cur.lastrowid


def bind(
    con: sqlite3.Connection,
    proposition_id: int,
    contract_id: int,
    match_quality: str,
    reviewer: str,
    note: str | None = None,
    now: str | None = None,
) -> None:
    """
    Bind a proposition to a contract, recording who judged the match and how well.

    `material_mismatch` is recorded rather than refused: under the paper-only
    posture the binding is research metadata, and knowing a contract measures
    something adjacent is more useful than pretending it measures nothing. What
    must never happen is a *live* position on a material mismatch, which §10.1
    covers and the absent order-management service enforces by construction.
    """
    if match_quality not in ("exact", "material_mismatch", "minor_divergence", "unreviewed"):
        raise RegistryError(f"unknown match_quality {match_quality!r}")
    con.execute(
        """INSERT OR REPLACE INTO proposition_contract_binding
           (proposition_id, contract_id, match_quality, reviewer, reviewed_at, note)
           VALUES (?,?,?,?,?,?)""",
        (proposition_id, contract_id, match_quality, reviewer,
         _canon(now) if now else _now(), note),
    )
    con.commit()


def tradeable_contracts(con: sqlite3.Connection) -> list[dict]:
    """Contracts whose eligibility currently permits opening a position."""
    cur = con.execute(
        "SELECT id, venue, market_id, outcome_token_id, deadline_utc, "
        "       eligibility_status, rules_version_hash "
        "FROM contracts WHERE eligibility_status='tradeable' ORDER BY id"
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def divergence_check(
    con: sqlite3.Connection,
    contract_id: int,
    current_rules_text: str,
) -> bool:
    """
    Has the venue amended the rules since this contract was registered?

    Returns True on divergence. Deliberately does *not* mutate anything: the
    caller records an adjudication and, if the change is material, registers the
    amended text as a new contract. Auto-disqualifying a standing forecast
    because someone appended a clarification is the over-correction §5.1 warns
    against.
    """
    row = con.execute(
        "SELECT rules_version_hash FROM contracts WHERE id=?", (contract_id,)
    ).fetchone()
    if not row:
        raise RegistryError(f"no contract {contract_id}")
    return row[0] != rules_version_hash(current_rules_text)

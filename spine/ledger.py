"""
Evidence ledger and forecast registry.

The two disciplines this module exists to enforce, both of which v1 stated in
prose and never implemented:

  1. **Availability-time retrieval** (v2 §8.1). A decision simulated at time T
     sees only what was available for decision at or before T — not what had
     merely *arrived* by then. An article present at 10:00 whose verification
     finishes at 10:05 is invisible to a 10:01 decision.

  2. **Commitment to inputs, not labels** (v2 §9.2). A forecast references a
     content-addressed manifest enumerating every input by hash, so changing what
     a version label points at breaks verification instead of silently altering
     what the forecast meant.
"""

from __future__ import annotations

import os
import sqlite3

from . import timeutil
from .canonical import GENESIS_HASH, content_hash
from .chain import compute_hash, head

SCHEMA_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "schema_v2.sql"
)

# Must match `PRAGMA user_version` in schema_v2.sql.
#   1  initial v2 schema
#   2  trade_decisions.mode; claim_contract_effects keyed by horizon and estimator
#   3  shadow execution (section 9) and collection (section 10) tables
#   4  canonical-timestamp CHECK constraints on every compared column
#   5  resolutions revised not replaced; scores keyed by resolution revision
SCHEMA_VERSION = 5


class LedgerError(RuntimeError):
    """A registration violated a discipline the ledger enforces."""


def connect(path: str = ":memory:", create: bool | None = None) -> sqlite3.Connection:
    """
    Open the ledger, refusing a database whose schema is not this one.

    `create` defaults to "create it if it is empty", which is what every caller
    wanted and several were getting wrong by testing `os.path.exists` — a file
    that exists is not the same as a file with tables in it.

    A database stamped with a different `user_version` is **refused**, not
    migrated and not opened anyway. Running new code against old tables makes a
    query return nothing where it should return rows, and an empty result is
    indistinguishable from evidence of absence. There is no real data in any of
    these databases yet, so the remedy is to recreate; when there is, this is
    where a migration goes.
    """
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")
    if path != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")

    has_tables = con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'").fetchone()[0] > 0

    if create is None:
        create = not has_tables
    if create and has_tables:
        raise LedgerError(f"{path} already has tables; refusing to re-create it")

    if create:
        with open(SCHEMA_PATH, encoding="utf-8") as fh:
            con.executescript(fh.read())
        return con

    if has_tables:
        found = con.execute("PRAGMA user_version").fetchone()[0]
        if found != SCHEMA_VERSION:
            con.close()
            raise LedgerError(
                f"{path} is schema version {found}, this code expects "
                f"{SCHEMA_VERSION}. Refusing to open it: new code against old "
                "tables returns empty results that read as evidence of absence. "
                "The schema is still changing and no database here holds real "
                "data yet, so recreate it.")
    return con


# ---------------------------------------------------------------------------
# point-in-time retrieval
# ---------------------------------------------------------------------------

def claims_available_at(con: sqlite3.Connection, as_of: str) -> list[dict]:
    """
    Claims usable by a decision made at `as_of`.

    Keys on available_for_decision_at, never first_seen_at. This is the single
    line that separates an honest backtest from a flattering one.
    """
    cur = con.execute(
        "SELECT id, claim_hash, assertion, establishes, n_eff_sources, "
        "       available_for_decision_at, feature_version "
        "FROM claims WHERE available_for_decision_at <= ? "
        "ORDER BY available_for_decision_at ASC",
        (as_of,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def effects_available_at(
    con: sqlite3.Connection, contract_id: int, as_of: str
) -> list[dict]:
    """
    Claim-to-contract effects usable at `as_of`, for one contract.

    Effects are per (claim, contract, horizon, model_version) — there is no
    context-free likelihood ratio, so retrieval is always contract-scoped.
    """
    cur = con.execute(
        "SELECT id, claim_id, horizon_days, llr, final_contribution, "
        "       contribution_cap, estimator, model_version, available_for_decision_at "
        "FROM claim_contract_effects "
        "WHERE contract_id = ? AND available_for_decision_at <= ? "
        "ORDER BY available_for_decision_at ASC",
        (contract_id, as_of),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# manifests
# ---------------------------------------------------------------------------

def put_manifest(con: sqlite3.Connection, kind: str, content: dict, created_at: str) -> str:
    """
    Store a content-addressed manifest and return its hash.

    Idempotent: the same content yields the same hash and is stored once. The
    manifest must enumerate inputs *by hash*, not by version label — see §9.2.
    """
    from .canonical import canonicalise

    h = content_hash(content)
    exists = con.execute(
        "SELECT 1 FROM manifests WHERE manifest_hash = ?", (h,)
    ).fetchone()
    if not exists:
        con.execute(
            "INSERT INTO manifests(manifest_hash, kind, content, created_at) VALUES(?,?,?,?)",
            (h, kind, canonicalise(content), created_at),
        )
        con.commit()
    return h


# ---------------------------------------------------------------------------
# forecast registration
# ---------------------------------------------------------------------------

_REQUIRED = (
    "proposition_id", "p_est_bp", "p_lo_bp", "p_hi_bp", "uncertainty_method",
    "min_width_bp", "p_base_bp", "forecast_kind", "reference_class_version_id",
    "inputs_manifest_hash", "model_version", "regime_id", "created_at", "source",
)

_OPTIONAL = ("contract_id", "p_market_bp", "source_detail", "label_available_at")


def register_forecast(con: sqlite3.Connection, **fields) -> str:
    """
    Register a forecast: compute its chain hash and insert it.

    Every substantive constraint — interval coherence, minimum width, chain
    continuity, reference class frozen first — lives in the schema, so this
    function stays thin on purpose. A guard implemented twice is a guard that
    eventually disagrees with itself.

    Returns the forecast hash.
    """
    missing = [k for k in _REQUIRED if fields.get(k) is None]
    if missing:
        raise LedgerError(f"missing required field(s): {', '.join(missing)}")
    unknown = set(fields) - set(_REQUIRED) - set(_OPTIONAL)
    if unknown:
        raise LedgerError(f"unknown field(s): {', '.join(sorted(unknown))}")

    if fields["forecast_kind"] == "market_conditioned" and fields.get("p_market_bp") is None:
        raise LedgerError(
            "a market_conditioned forecast must record the market price it was "
            "conditioned on; otherwise the comparison is unreconstructable"
        )
    if fields["forecast_kind"] == "independent" and fields.get("p_market_bp") is not None:
        raise LedgerError(
            "an independent forecast must not carry a market price: it is defined "
            "as having been produced without seeing one"
        )

    # created_at is hash-committed, so it is validated rather than coerced: a
    # write path that quietly reshaped a committed field would change what the
    # chain attests to. The other timestamp columns in this project are
    # canonicalised on write; this one is the exception, deliberately.
    for field_name in ("created_at", "label_available_at"):
        value = fields.get(field_name)
        if value is not None and not timeutil.is_canonical(value):
            raise LedgerError(
                f"{field_name}={value!r} is not canonical "
                f"({timeutil.SQL_GLOB}). Point-in-time retrieval compares these "
                "as strings, and a whole-second stamp sorts after the "
                "millisecond form of the same instant — so the forecast would "
                "be invisible to a decision made at that moment. Use "
                "spine.timeutil.iso()")

    if not con.execute(
        "SELECT 1 FROM manifests WHERE manifest_hash = ?",
        (fields["inputs_manifest_hash"],),
    ).fetchone():
        raise LedgerError(
            f"manifest {fields['inputs_manifest_hash'][:12]}... is not stored; "
            "register inputs before the forecast that commits to them"
        )

    prev = head(con)
    row = {k: fields.get(k) for k in (*_REQUIRED, *_OPTIONAL)}
    fh = compute_hash(row, prev)

    cols = ["forecast_hash", "prev_hash", *_REQUIRED, *_OPTIONAL]
    vals = [fh, prev, *(row[k] for k in _REQUIRED), *(row[k] for k in _OPTIONAL)]
    con.execute(
        f"INSERT INTO forecasts({','.join(cols)}) "
        f"VALUES({','.join('?' * len(cols))})",
        vals,
    )
    con.commit()
    return fh


def reconstruct(con: sqlite3.Connection, forecast_hash: str) -> dict:
    """
    Return everything needed to reproduce a forecast from stored inputs alone.

    v2 §12's reproducibility criterion is *auditable*, not necessarily
    re-runnable: for LLM-assisted forecasts the manifest holds prompts, retrieved
    context and raw output, because inference endpoints drift and retire.
    """
    cur = con.execute(
        "SELECT f.*, m.content AS manifest_content, m.kind AS manifest_kind "
        "FROM forecasts f JOIN manifests m ON m.manifest_hash = f.inputs_manifest_hash "
        "WHERE f.forecast_hash = ?",
        (forecast_hash,),
    )
    r = cur.fetchone()
    if r is None:
        raise LedgerError(f"no forecast {forecast_hash[:12]}...")
    row = dict(zip([d[0] for d in cur.description], r))

    rc = con.execute(
        "SELECT class_name, version, n, k, alpha, beta, frozen_at, selection_rule_ref "
        "FROM reference_class_versions WHERE id = ?",
        (row["reference_class_version_id"],),
    ).fetchone()
    row["reference_class"] = dict(
        zip(
            ["class_name", "version", "n", "k", "alpha", "beta", "frozen_at",
             "selection_rule_ref"],
            rc,
        )
    ) if rc else None

    row["effects_at_decision"] = (
        effects_available_at(con, row["contract_id"], row["created_at"])
        if row["contract_id"] is not None else []
    )
    return row

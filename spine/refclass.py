"""
Freezing a reference class, and getting it back.

Every forecast in this project rests on one. `reference_class_versions` and
`reference_class_members` have existed since schema v2 with **no Python writer**:
the tests created them with raw SQL and nothing else could. That is the same
pattern that hid `scores`, `settlements` and `forecast_edges` — a table defined,
a discipline documented, and no code that makes either true.

Four things this module refuses, each of which turns a reference class into a
number that merely looks like one.

**`k` and `n` are derived from the roster, never passed in.** A caller that
supplies its own counts can supply counts that disagree with the members — which
is how a class quietly stops describing its own membership while still
presenting a base rate.

**Members must predate the freeze.** The schema already refuses a forecast whose
class was frozen after it; this closes the other end, where a member is added to
a class after the moment it was supposedly fixed.

**The selection rule is content-addressed, not a label.** §9.3 is blunt that
`frozen_at < created_at` is "necessary and wildly insufficient" for "the analyst
had not seen the case" — the control is the predeclared rule, not the timestamp. A free-text
`selection_rule_ref` is a label, and §9.2 is explicit that committing to labels
rather than content is how the meaning of a record changes without the record
changing. The rule goes in a manifest and the class commits to its hash.

**Exposure is stated, not inferred.** `exposure_units` is the denominator the
hazard divides by, and it is a modelling decision about what one unit of
exposure *is* — forty case-weeks and forty case-years are the same `n` and a
tenfold difference in rate. It is stored alongside a name for the unit, so the
number is interpretable a year later by someone who was not there.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import timeutil
from .ledger import put_manifest
from .models import ReferenceClass

_now = timeutil.now
_canon = timeutil.canonical


class RefClassError(RuntimeError):
    """A reference class as requested would not mean what it claims."""


@dataclass(frozen=True)
class Member:
    """One case in the class. Immutable within a version, by trigger."""
    event_name: str
    event_date: str
    outcome: int          # 1 if the event occurred, 0 if it did not
    source_url: str | None = None


def declare_selection_rule(
    con: sqlite3.Connection,
    rule: dict,
    created_at: str | None = None,
) -> str:
    """
    Store a selection rule and return its hash.

    Content-addressed for the reason §9.2 gives: a class that referenced a rule
    by name could have that name repointed, and every forecast built on the
    class would silently come to mean something else. The hash cannot be
    repointed; changing the rule changes the hash and breaks the link visibly.
    """
    if not isinstance(rule, dict) or not rule:
        raise RefClassError("a selection rule must be a non-empty structure")
    return put_manifest(con, "model_config", rule,
                        _canon(created_at) if created_at else _now())


def freeze(
    con: sqlite3.Connection,
    *,
    class_name: str,
    version: int,
    description: str,
    members: list[Member],
    selection_rule_ref: str,
    alpha: float,
    beta: float,
    prior_justification: str,
    exposure_units: float,
    exposure_unit_name: str,
    frozen_at: str | None = None,
    exposure_window_days: int | None = None,
    censoring_note: str | None = None,
) -> int:
    """
    Freeze a version of a reference class. Returns its id.

    `k` and `n` come from `members`. The counts and the roster cannot disagree
    because there is only one of them.
    """
    ts = _canon(frozen_at) if frozen_at else _now()
    if alpha <= 0 or beta <= 0:
        raise RefClassError("a Beta prior needs alpha > 0 and beta > 0")
    if exposure_units <= 0:
        raise RefClassError(
            "exposure_units must be positive: it is the denominator the hazard "
            "divides by, and a class without one can only produce a static rate")
    if not exposure_unit_name.strip():
        raise RefClassError(
            "name the exposure unit. Forty case-weeks and forty case-years are "
            "the same n and a tenfold difference in rate, and in a year nobody "
            "will remember which this was")
    if not prior_justification.strip():
        raise RefClassError(
            "justify the prior. Beta(1,3) has mean 0.25 and is not a rare-event "
            "default, which is the mistake this field exists to catch")
    if not con.execute("SELECT 1 FROM manifests WHERE manifest_hash=?",
                       (selection_rule_ref,)).fetchone():
        raise RefClassError(
            f"selection rule {selection_rule_ref[:12]}... is not stored. Declare "
            "the rule before the class it selected, not after")

    names = [m.event_name for m in members]
    if len(set(names)) != len(names):
        raise RefClassError("duplicate member names; the roster would be ambiguous")
    late = [m.event_name for m in members if _canon(m.event_date) > ts]
    if late:
        raise RefClassError(
            f"member(s) {late[:3]} occur after the freeze time. A class frozen "
            "before the cases it contains is a class selected on outcomes")
    bad = [m.event_name for m in members if m.outcome not in (0, 1)]
    if bad:
        raise RefClassError(f"member(s) {bad[:3]} have a non-binary outcome")

    n = len(members)
    k = sum(m.outcome for m in members)

    try:
        cur = con.execute(
            """INSERT INTO reference_class_versions
               (class_name, version, description, n, k, alpha, beta,
                prior_justification, exposure_units, exposure_unit_name,
                exposure_window_days, censoring_note, frozen_at,
                selection_rule_ref)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (class_name, version, description, n, k, alpha, beta,
             prior_justification, exposure_units, exposure_unit_name,
             exposure_window_days, censoring_note, ts, selection_rule_ref))
    except sqlite3.IntegrityError as e:
        raise RefClassError(
            f"{class_name} v{version} already exists. Versions are immutable; "
            f"a changed roster or prior is v{version + 1}") from e

    cid = cur.lastrowid
    for m in members:
        con.execute(
            """INSERT INTO reference_class_members
               (class_version_id, event_name, event_date, outcome, added_at,
                source_url) VALUES (?,?,?,?,?,?)""",
            (cid, m.event_name, _canon(m.event_date), m.outcome, ts, m.source_url))
    con.commit()
    return cid


def load(con: sqlite3.Connection, class_version_id: int) -> ReferenceClass:
    """
    The stored class as the model consumes it.

    This is the function whose absence made deadline-aware forecasts
    unreconstructable: `exposure_units` is the hazard's denominator, and until
    it was stored there was nothing to load it from.
    """
    row = con.execute(
        "SELECT class_name, version, k, n, exposure_units, alpha, beta "
        "FROM reference_class_versions WHERE id=?", (class_version_id,)).fetchone()
    if not row:
        raise RefClassError(f"no reference class version {class_version_id}")
    return ReferenceClass(name=row[0], version=row[1], k=row[2], n=row[3],
                          exposure_units=row[4], alpha=row[5], beta=row[6])


def latest(con: sqlite3.Connection, class_name: str,
           as_of: str | None = None) -> int | None:
    """
    The newest version of a class frozen by `as_of`.

    Point-in-time like everything else: a forecast simulated last March must use
    the roster as it stood in March, not the one improved since.
    """
    ts = _canon(as_of) if as_of else _now()
    row = con.execute(
        "SELECT id FROM reference_class_versions WHERE class_name=? AND "
        "frozen_at <= ? ORDER BY version DESC LIMIT 1", (class_name, ts)).fetchone()
    return row[0] if row else None


def members(con: sqlite3.Connection, class_version_id: int) -> list[Member]:
    """The roster, so a base rate can be audited against the cases behind it."""
    cur = con.execute(
        "SELECT event_name, event_date, outcome, source_url "
        "FROM reference_class_members WHERE class_version_id=? "
        "ORDER BY event_date, event_name", (class_version_id,))
    return [Member(*r) for r in cur.fetchall()]


def audit(con: sqlite3.Connection, class_version_id: int) -> dict:
    """
    Check a stored class against its own roster.

    Cheap, and worth running before anything is built on a class: the counts and
    the members are stored separately, so they can in principle drift — by a
    direct write, a restore, a migration. The triggers make it hard; this makes
    it visible.
    """
    row = con.execute(
        "SELECT class_name, version, n, k, exposure_units, frozen_at, "
        "       selection_rule_ref FROM reference_class_versions WHERE id=?",
        (class_version_id,)).fetchone()
    if not row:
        raise RefClassError(f"no reference class version {class_version_id}")
    roster = members(con, class_version_id)
    actual_n, actual_k = len(roster), sum(m.outcome for m in roster)
    rule = con.execute("SELECT 1 FROM manifests WHERE manifest_hash=?",
                       (row[6],)).fetchone()
    problems = []
    if actual_n != row[2]:
        problems.append(f"n is {row[2]} but the roster holds {actual_n}")
    if actual_k != row[3]:
        problems.append(f"k is {row[3]} but the roster has {actual_k} events")
    if not rule:
        problems.append("the selection rule manifest is missing")
    late = [m.event_name for m in roster if m.event_date > row[5]]
    if late:
        problems.append(f"{len(late)} member(s) postdate the freeze")
    return {"class": f"{row[0]} v{row[1]}", "n": actual_n, "k": actual_k,
            "exposure_units": row[4], "ok": not problems, "problems": problems}

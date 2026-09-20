"""
Health leases: permission that lapses on its own.

§11.1: "Order entry is gated by an **expiring health lease**, not a persistent
'healthy' flag: a dead monitor cannot clear a flag, but a lease fails safe on
its own."

The distinction is the entire mechanism and it is easy to read past. A flag
needs something alive to turn it off — and the failure that most urgently needs
it turned off is that thing dying. An expiry is written at grant time and read
at use time, so permission lapses without anyone acting.

Three things follow, and all three are enforced rather than described.

**A lease cannot be extended.** `UPDATE ... SET expires_at` is refused by
trigger. Extending is the flag behaviour wearing a lease's clothes: it converts
"this was true a moment ago" into "this is true until further notice". Granting
a new lease means checking again, which is the point.

**A lease states what was checked.** `basis` is required. A lease with no stated
basis is a flag with a timer on it.

**Expiry and revocation are different events.** Expiry is the absence of a
decision; revocation is a decision, and it names who made it and why. A halt is
not the same thing as a monitor going quiet, and a record that conflates them
cannot answer the question worth asking afterwards.

This module grants and reads leases. It does not decide *whether* systems are
healthy — that judgement belongs to whatever ran the checks, and folding it in
here would let one component both assess and authorise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from . import timeutil

# Long enough to survive an ordinary collection cycle, short enough that a dead
# collector stops mattering within one.
DEFAULT_TTL_SECONDS = 900.0

# How far ahead of the clock a grant may be stamped. Small, because the only
# legitimate cause is skew between whatever ran the checks and the database
# host; anything larger is a lease that would arm itself later.
MAX_GRANT_SKEW_SECONDS = 60.0

ALL_SCOPES = "*"


class LeaseError(RuntimeError):
    """A lease operation would have granted permission it should not."""


@dataclass(frozen=True)
class Lease:
    id: int
    scope: str
    granted_at: str
    expires_at: str
    granted_by: str
    basis: str
    revoked_at: str | None = None
    revocation_reason: str | None = None

    def live_at(self, as_of: str) -> bool:
        ts = timeutil.canonical(as_of)
        if self.revoked_at is not None and ts >= self.revoked_at:
            return False
        return self.granted_at <= ts < self.expires_at

    def summary(self) -> str:
        state = ("revoked" if self.revoked_at else "granted")
        return (f"lease {self.id} [{self.scope}] {state} by {self.granted_by}, "
                f"expires {self.expires_at}")


def grant(
    con: sqlite3.Connection,
    *,
    granted_by: str,
    basis: str,
    scope: str = ALL_SCOPES,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    granted_at: str | None = None,
) -> Lease:
    """
    Grant a lease that expires by itself.

    `basis` records what was actually checked. It is required because a lease
    granted without stating what it rests on carries no more information than
    the flag it replaced.
    """
    if ttl_seconds <= 0:
        raise LeaseError("a lease with no duration authorises nothing")
    if not granted_by.strip():
        raise LeaseError(
            "a lease must name who granted it; permission nobody is "
            "accountable for is the flag this replaces")
    if not basis.strip():
        raise LeaseError(
            "state what was checked. A lease with no basis is a flag with a "
            "timer on it")

    start = timeutil.canonical(granted_at) if granted_at else timeutil.now()
    # A lease attests to a check that happened, and a check cannot have happened
    # in the future. Without this, a grant stamped by a clock running fast is a
    # permission that switches itself ON later with nobody acting -- the exact
    # inverse of the property this module exists for. Measured: a lease stamped
    # two hours ahead reads as not-permitted now and permitted in two hours.
    skew = (timeutil.parse(start) - timeutil.parse(timeutil.now())).total_seconds()
    if skew > MAX_GRANT_SKEW_SECONDS:
        raise LeaseError(
            f"granted_at is {skew:.0f}s in the future, beyond the "
            f"{MAX_GRANT_SKEW_SECONDS:.0f}s skew allowance. A lease attests to a "
            "check that happened; one dated ahead would arm itself later, which "
            "is the opposite of failing safe")
    expires = timeutil.iso(timeutil.parse(start) + timedelta(seconds=ttl_seconds))
    cur = con.execute(
        """INSERT INTO health_leases
           (scope, granted_at, expires_at, granted_by, basis)
           VALUES (?,?,?,?,?)""",
        (scope, start, expires, granted_by, basis))
    con.commit()
    return Lease(cur.lastrowid, scope, start, expires, granted_by, basis)


def revoke(
    con: sqlite3.Connection,
    lease_id: int,
    *,
    revoked_by: str,
    reason: str,
    revoked_at: str | None = None,
) -> None:
    """
    End one specific lease before its expiry.

    **This is not a halt**, and the distinction cost a test failure to find:
    leases overlap during renewal, so ending one while another covering the same
    scope is still live stops nothing at all. §11.1's halt means *no new
    exposure*, which is a statement about permission and not about a row. Use
    `halt()`.

    Revocation is kept distinct from expiry throughout. A halt is a decision
    someone took; a lapse is the absence of one. Those call for different
    responses and a single 'not permitted' state cannot tell them apart
    afterwards.
    """
    if not revoked_by.strip() or not reason.strip():
        raise LeaseError("a revocation names who halted and why")
    ts = timeutil.canonical(revoked_at) if revoked_at else timeutil.now()
    n = con.execute(
        "UPDATE health_leases SET revoked_at=?, revoked_by=?, "
        "revocation_reason=? WHERE id=? AND revoked_at IS NULL",
        (ts, revoked_by, reason, lease_id)).rowcount
    if not n:
        raise LeaseError(f"no unrevoked lease {lease_id}")
    con.commit()


def halt(
    con: sqlite3.Connection,
    *,
    halted_by: str,
    reason: str,
    scope: str = ALL_SCOPES,
    halted_at: str | None = None,
) -> int:
    """
    Stop action in `scope`: revoke every lease currently authorising it.

    This is what §11.1 means by halt. Revoking a single lease does not achieve
    it, because renewal leaves leases overlapping — end one and the previous or
    the next may still cover the same instant. A halt has to be a statement
    about permission, so it ends everything that grants it.

    Halting the global scope also ends scoped leases, since a scoped lease is a
    narrower permission and a global halt is meant to be the broadest possible
    'no'. Returns the number of leases ended.
    """
    if not halted_by.strip() or not reason.strip():
        raise LeaseError("a halt names who ordered it and why")
    ts = timeutil.canonical(halted_at) if halted_at else timeutil.now()
    # The scope condition is parameterised rather than interpolated. Building
    # the clause as a string would work and would also make this the one place
    # in the project where a WHERE fragment is assembled in Python, which is a
    # category the SQL check in tests/test_untrusted.py deliberately does not
    # allow anywhere.
    n = con.execute(
        """UPDATE health_leases SET revoked_at=?, revoked_by=?,
                  revocation_reason=?
           WHERE (? = ? OR scope IN (?, ?)) AND revoked_at IS NULL
             AND granted_at <= ? AND expires_at > ?""",
        (ts, halted_by, reason, scope, ALL_SCOPES, scope, ALL_SCOPES,
         ts, ts)).rowcount
    con.commit()
    return n


def current(con: sqlite3.Connection, as_of: str | None = None,
            scope: str = ALL_SCOPES) -> Lease | None:
    """
    The lease authorising `scope` at `as_of`, or None.

    A scope falls back to the global lease, so a global halt stops everything
    without needing to enumerate what it stopped.
    """
    ts = timeutil.canonical(as_of) if as_of else timeutil.now()
    for key in dict.fromkeys((scope, ALL_SCOPES)):
        row = con.execute(
            """SELECT id, scope, granted_at, expires_at, granted_by, basis,
                      revoked_at, revocation_reason
               FROM health_leases
               WHERE scope = ? AND granted_at <= ? AND expires_at > ?
                 AND (revoked_at IS NULL OR revoked_at > ?)
               ORDER BY expires_at DESC LIMIT 1""", (key, ts, ts, ts)).fetchone()
        if row:
            return Lease(*row)
    return None


def permitted(con: sqlite3.Connection, as_of: str | None = None,
              scope: str = ALL_SCOPES) -> tuple[bool, str]:
    """
    Whether action is permitted, and why not when it is not.

    The reason distinguishes the three states that matter: no lease was ever
    granted, one was granted and lapsed, or one was revoked by a named person.
    A bare False cannot be acted on.
    """
    ts = timeutil.canonical(as_of) if as_of else timeutil.now()
    if current(con, ts, scope) is not None:
        return True, "lease is live"

    row = con.execute(
        """SELECT expires_at, revoked_at, revoked_by, revocation_reason
           FROM health_leases WHERE scope IN (?, ?) AND granted_at <= ?
           ORDER BY granted_at DESC LIMIT 1""", (scope, ALL_SCOPES, ts)).fetchone()
    if row is None:
        return False, ("no health lease has ever been granted for scope "
                       f"{scope!r}; nothing has attested that the system is fit "
                       "to act")
    expires, revoked_at, revoked_by, reason = row
    if revoked_at is not None and revoked_at <= ts:
        return False, (f"halted by {revoked_by} at {revoked_at}: {reason}")
    return False, (f"the last lease for scope {scope!r} expired at {expires}. "
                   "Nothing revoked it — it lapsed, which is what a lease does "
                   "when whatever was meant to renew it stopped")


def pending(con: sqlite3.Connection, as_of: str | None = None,
            scope: str = ALL_SCOPES) -> list[dict]:
    """
    Leases dated after `as_of` — permission that would arm itself later.

    `grant()` refuses to create one, but a database restored from elsewhere, or
    written before that guard existed, can hold them. Worth being able to ask,
    because a lease nobody granted *today* becoming live tomorrow is the
    hardest kind of permission to notice.
    """
    ts = timeutil.canonical(as_of) if as_of else timeutil.now()
    cur = con.execute(
        "SELECT id, scope, granted_at, expires_at, granted_by, basis "
        "FROM health_leases WHERE granted_at > ? AND scope IN (?, ?) "
        "AND revoked_at IS NULL ORDER BY granted_at", (ts, scope, ALL_SCOPES))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

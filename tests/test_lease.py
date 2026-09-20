#!/usr/bin/env python3
"""
Health leases (§11.1): permission that lapses without anyone acting.

"A dead monitor cannot clear a flag, but a lease fails safe on its own." The
suite is built around that sentence, plus the concrete failure it describes: a
collector that stops leaves everything downstream acting on stale state, and
nothing has to go wrong actively for that to happen.

Run: python3 tests/test_lease.py
Stdlib only.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import decision, ledger, lease, registry, shadow, timeutil  # noqa: E402
from spine.lease import LeaseError  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 9, 20, 8, 0, 0, tzinfo=timezone.utc)


def ok(label, cond, detail=""):
    (PASS if cond else FAIL).append(label)
    print(f"  {'PASS' if cond else 'FAIL'}  {label}"
          f"{('  [' + str(detail) + ']') if detail and not cond else ''}")


def raises(fn, exc=Exception):
    try:
        fn()
        return False
    except exc:
        return True
    except Exception:
        return False


def at(**kw):
    return timeutil.iso(NOW + timedelta(**kw))


def main() -> int:
    print("=" * 76)
    print("SPINE HEALTH LEASES (§11.1)")
    print("=" * 76)

    con = ledger.connect(":memory:", create=True)

    print("\n[1] Nothing is permitted until something attests to it\n")
    perm, why = lease.permitted(con, at())
    ok("with no lease ever granted, nothing is permitted", not perm)
    ok("...and the reason distinguishes 'never granted' from 'lapsed'",
       "has ever been granted" in why, why)

    ok("a lease with no duration is refused",
       raises(lambda: lease.grant(con, granted_by="a", basis="b",
                                  ttl_seconds=0), LeaseError))
    ok("a lease with no grantor is refused",
       raises(lambda: lease.grant(con, granted_by="  ", basis="b"), LeaseError))
    ok("a lease with no stated basis is refused",
       raises(lambda: lease.grant(con, granted_by="a", basis=""), LeaseError))
    ok("...because that is a flag with a timer on it",
       "flag with a timer" in _msg(
           lambda: lease.grant(con, granted_by="a", basis="")))

    print("\n[2] A lease lapses without anyone acting\n")
    l1 = lease.grant(con, granted_by="monitor-1",
                     basis="collector ran 2m ago; 448 books recorded; no failed passes",
                     ttl_seconds=900, granted_at=at())
    ok("a lease grants", l1.id > 0)
    ok("it is live at the moment of granting", l1.live_at(at()))
    ok("it is live within its window", l1.live_at(at(minutes=14)))
    ok("it is NOT live past its expiry", not l1.live_at(at(minutes=16)))
    ok("...and nothing had to happen for that", lease.current(con, at(minutes=16))
       is None)
    ok("it is not live before it was granted", not l1.live_at(at(minutes=-1)))

    perm, why = lease.permitted(con, at(minutes=5))
    ok("action is permitted inside the window", perm, why)
    perm, why = lease.permitted(con, at(minutes=30))
    ok("action is not permitted outside it", not perm)
    ok("...and the reason says it lapsed rather than being revoked",
       "lapsed" in why and "Nothing revoked it" in why, why)
    print(f"        {why}")

    print("\n[3] A lease cannot be extended, only replaced\n")
    ok("extending the expiry is refused by the schema",
       raises(lambda: con.execute(
           "UPDATE health_leases SET expires_at=? WHERE id=?",
           (at(days=30), l1.id)), sqlite3.IntegrityError))
    ok("...because extending is the flag behaviour wearing a lease's clothes",
       "grant a new one after checking again" in _msg(lambda: con.execute(
           "UPDATE health_leases SET expires_at=? WHERE id=?",
           (at(days=30), l1.id))))
    l2 = lease.grant(con, granted_by="monitor-1", basis="re-checked; still fresh",
                     ttl_seconds=900, granted_at=at(minutes=10))
    ok("granting a new one is how renewal works", l2.id != l1.id)
    ok("...and the new window covers what the old one did not",
       lease.current(con, at(minutes=20)) is not None)
    ok("a lease is never deleted, because it is the audit trail",
       raises(lambda: con.execute("DELETE FROM health_leases WHERE id=?",
                                  (l1.id,)), sqlite3.IntegrityError))

    print("\n[4] Halting is a decision; expiring is the absence of one\n")
    # Renewal leaves leases overlapping, so ending ONE stops nothing while
    # another still covers the instant. This is what separates revoke() from
    # halt(), and it took a failing test to notice.
    lease.revoke(con, l2.id, revoked_by="analyst-1",
                 reason="venue reported a settlement dispute",
                 revoked_at=at(minutes=12))
    ok("revoking one lease does NOT halt, because the previous one still covers",
       lease.current(con, at(minutes=13)) is not None,
       "l1 expires at +15 and was never revoked")
    ok("...which is why a halt is a separate operation",
       lease.permitted(con, at(minutes=13))[0])

    stopped = lease.halt(con, halted_by="analyst-1",
                         reason="venue reported a settlement dispute on three "
                                "open questions",
                         halted_at=at(minutes=13))
    ok("a halt ends every lease authorising the scope", stopped >= 1, stopped)
    ok("...and nothing is permitted afterwards",
       not lease.permitted(con, at(minutes=14))[0])
    perm, why = lease.permitted(con, at(minutes=14))
    ok("the reason names who halted and why",
       "analyst-1" in why and "settlement dispute" in why, why)
    ok("a halt is distinguishable from a lapse", "halted by" in why)
    ok("...even though the lease had not expired",
       timeutil.parse(l1.expires_at) > timeutil.parse(at(minutes=14)))
    ok("a halt with no reason is refused",
       raises(lambda: lease.halt(con, halted_by="x", reason=""), LeaseError))
    ok("a halt with nothing live to stop ends nothing, harmlessly",
       lease.halt(con, halted_by="x", reason="already halted",
                  halted_at=at(minutes=15)) == 0)
    ok("a revocation with no reason is refused",
       raises(lambda: lease.revoke(con, l1.id, revoked_by="x", reason=""),
              LeaseError))
    ok("revoking an already-revoked lease is refused",
       raises(lambda: lease.revoke(con, l2.id, revoked_by="x", reason="y"),
              LeaseError))
    ok("the schema refuses a revocation with no revoker",
       raises(lambda: con.execute(
           "INSERT INTO health_leases(scope,granted_at,expires_at,granted_by,"
           "basis,revoked_at) VALUES('*',?,?,'a','b',?)",
           (at(), at(hours=1), at(minutes=30))), sqlite3.IntegrityError))
    print(f"        {why}")

    print("\n[5] Scope falls back, so a global halt stops everything\n")
    con2 = ledger.connect(":memory:", create=True)
    lease.grant(con2, granted_by="m", basis="all systems checked",
                scope="*", ttl_seconds=900, granted_at=at())
    ok("a scoped request falls back to the global lease",
       lease.current(con2, at(minutes=1), scope="committee_adoption") is not None)
    narrow = lease.grant(con2, granted_by="m", basis="family-specific check",
                         scope="committee_adoption", ttl_seconds=900,
                         granted_at=at())
    ok("a scoped lease is preferred when present",
       lease.current(con2, at(minutes=1),
                     scope="committee_adoption").id == narrow.id)
    lease.halt(con2, halted_by="analyst-2", reason="global stop",
               halted_at=at(minutes=2))
    ok("a global halt ends scoped leases too",
       lease.current(con2, at(minutes=3), scope="committee_adoption") is None)
    ok("...because a global halt is meant to be the broadest possible no",
       not lease.permitted(con2, at(minutes=3), scope="committee_adoption")[0])

    print("\n[6] The bug this was missing: a stale book priced as a live one\n")
    con3 = ledger.connect(":memory:", create=True)
    registry.ingest_markets(con3, [{
        "id": "mk-l", "question": "q", "end_date": at(days=9),
        "rules_text": "r", "outcome_token_id": "t"}], jurisdiction="GB",
        now=at())
    cid = con3.execute("SELECT id FROM contracts").fetchone()[0]
    shadow.record_book(con3, cid, [(5900, 500.0)], [(6100, 500.0)],
                       captured_at=at(days=-3), source="fixture")
    ok("a three-day-old book is refused",
       shadow.book_at(con3, cid, at()) is None)
    ok("...and its age is reportable, so the refusal is diagnosable",
       shadow.book_age_seconds(con3, cid, at()) == 3 * 86400.0)
    ok("a stale book can still be retrieved deliberately, for study",
       shadow.book_at(con3, cid, at(), max_age_seconds=None) is not None)
    shadow.record_book(con3, cid, [(5950, 500.0)], [(6050, 500.0)],
                       captured_at=at(minutes=-2), source="fixture")
    ok("a fresh book is returned", shadow.book_at(con3, cid, at()) is not None)
    ok("...and it is the fresh one, not the stale one",
       shadow.book_at(con3, cid, at()).best_bid_bp == 5950)

    print("\n[7] A decision without a live lease\n")
    book = [decision.BookLevel(6100, 500.0)]
    kw = dict(p_est_bp=7000, p_lo_bp=6500, p_hi_bp=7500, side="YES", book=book,
              intended_size_usd=100.0, costs_bp=10.0,
              eligibility_status="tradeable", max_notional_usd=1000.0,
              cluster_exposure_used_usd=0.0, cluster_exposure_cap_usd=1000.0)
    d_live = decision.decide(**kw, mode=decision.LIVE, lease_ok=False,
                             lease_reason="the last lease expired at 08:15")
    ok("a LIVE decision without a lease abstains",
       not d_live.permitted and "health lease" in (d_live.abstain_reason or ""),
       d_live.abstain_reason)
    d_paper = decision.decide(**kw, mode=decision.PAPER, lease_ok=False,
                              lease_reason="the last lease expired at 08:15")
    ok("a PAPER decision proceeds", d_paper.permitted, d_paper.abstain_reason)
    ok("...but records that a live run would have abstained",
       any("would have abstained" in n for n in d_paper.notes), d_paper.notes)
    ok("...so the paper run does not overstate what live could have done",
       any("no live health lease" in n for n in d_paper.notes))
    d_ok = decision.decide(**kw, mode=decision.LIVE, lease_ok=True)
    ok("with a live lease the LIVE decision proceeds", d_ok.permitted,
       d_ok.abstain_reason)
    ok("the lease is checked before eligibility, since a system that cannot "
       "attest to itself should not assess a market",
       "health lease" in (decision.decide(
           **{**kw, "eligibility_status": "close_only"}, mode=decision.LIVE,
           lease_ok=False, lease_reason="lapsed").abstain_reason or ""))

    print("\n" + "=" * 76)
    print(f"{len(PASS)}/{len(PASS) + len(FAIL)} checks passed")
    if FAIL:
        print("\nFAILED:")
        for f_ in FAIL:
            print(f"  - {f_}")
    print("=" * 76)
    return 0 if not FAIL else 1


def _msg(fn) -> str:
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the message is the thing under test
        return str(e)
    return ""


if __name__ == "__main__":
    sys.exit(main())

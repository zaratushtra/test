#!/usr/bin/env python3
"""
Temporal splits (§8.3): the leak that a date split does not close.

A forecast is made at one time and resolves at another, so an example on the
training side by creation date may only become labelled long after the cutoff.
Training on it fits an answer that did not exist.

Run: python3 tests/test_splits.py
Stdlib only.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from spine import ledger, splits, timeutil  # noqa: E402
from spine.splits import Example, SplitError  # noqa: E402

PASS, FAIL = [], []
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


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


def at(days):
    return timeutil.iso(NOW + timedelta(days=days))


def main() -> int:
    print("=" * 76)
    print("SPINE TEMPORAL SPLITS (§8.3)")
    print("=" * 76)

    print("\n[1] The leak a date split leaves open\n")
    # Two forecasts made the same day. One resolves in a week, one in a year.
    # A naive split by creation date puts BOTH in training.
    quick = Example("quick", at(10), at(17))
    slow = Example("slow", at(10), at(375))
    s = splits.split([quick, slow], at(100), embargo_seconds=0,
                     min_retention=0.0)
    ok("the fast-resolving example trains", [e.key for e in s.train] == ["quick"])
    ok("the slow one is PURGED, not trained on",
       [e.key for e in s.purged] == ["slow"], s.summary())
    ok("...and it is not test data either, because the model saw the question",
       not any(e.key == "slow" for e in s.test))
    ok("a naive creation-date split would have trained on both",
       quick.created_at == slow.created_at)
    print(f"        both created {at(10)[:10]}; "
          f"one labelled {at(17)[:10]}, the other {at(375)[:10]}")

    print("\n[2] Three groups, all named and all counted\n")
    pool = (
        [Example(f"tr{i}", at(i), at(i + 5)) for i in range(40)]         # train
        + [Example(f"pu{i}", at(i), at(200 + i)) for i in range(5)]      # purged
        + [Example(f"em{i}", at(51 + i), at(60 + i)) for i in range(3)]  # embargo
        + [Example(f"te{i}", at(70 + i), at(80 + i)) for i in range(10)] # test
        + [Example("open", at(20), None)]                               # unlabelled
    )
    s2 = splits.split(pool, at(50), embargo_seconds=7 * 86400)
    ok("training holds only pre-cutoff, pre-labelled examples",
       len(s2.train) == 40, len(s2.train))
    ok("late-resolving examples are purged", len(s2.purged) == 5)
    ok("examples just after the cutoff are embargoed, not tested on",
       len(s2.embargoed) == 3, [e.key for e in s2.embargoed])
    ok("test holds only what is clear of the embargo", len(s2.test) == 10)
    ok("an unlabelled example is neither trained nor tested on",
       len(s2.unlabelled) == 1 and s2.unlabelled[0].key == "open")
    ok("...because assuming it resolvable would move open questions into training",
       not any(e.key == "open" for e in s2.train + s2.test))
    ok("the purge rate is reported", abs(s2.purge_rate - 5 / 45) < 1e-9,
       s2.purge_rate)
    print(f"        {s2.summary()}")

    print("\n[3] The embargo, and why dropping overlaps is not enough\n")
    just_after = [Example("j", at(51), at(55))]
    ok("with an embargo the example is held out",
       len(splits.split(just_after, at(50)).embargoed) == 1)
    ok("without one it would be tested on",
       len(splits.split(just_after, at(50), embargo_seconds=0).test) == 1)
    ok("a longer embargo holds out more",
       len(splits.split(pool, at(50), embargo_seconds=30 * 86400).test)
       < len(s2.test))
    ok("a negative embargo is refused",
       raises(lambda: splits.split(pool, at(50), embargo_seconds=-1),
              SplitError))

    print("\n[4] A split that keeps almost nothing is refused\n")
    mostly_slow = ([Example(f"s{i}", at(i), at(500 + i)) for i in range(40)]
                   + [Example(f"f{i}", at(i), at(i + 2)) for i in range(3)])
    ok("a heavy purge raises rather than reporting on what is left",
       raises(lambda: splits.split(mostly_slow, at(50)), SplitError))
    ok("...and says the sample would be selected by resolution speed",
       "selected by how fast its questions resolved" in _msg(
           lambda: splits.split(mostly_slow, at(50))))
    ok("it can be allowed deliberately, when the horizon cost IS the question",
       splits.split(mostly_slow, at(50), min_retention=0.0).retention < 0.1)
    ok("an empty pool does not raise",
       splits.split([], at(50)).summary().startswith("train 0"))

    print("\n[5] Verifying a split after the fact\n")
    splits.assert_no_leakage(s2)
    ok("a clean split verifies", True)
    leaky = splits.Split(at(50), train=[Example("bad", at(10), at(90))])
    ok("a training example labelled after the cutoff is caught",
       raises(lambda: splits.assert_no_leakage(leaky), SplitError))
    ok("...and the message says it fits an answer that did not exist",
       "an answer that did not exist" in _msg(
           lambda: splits.assert_no_leakage(leaky)))
    early_test = splits.Split(at(50), test=[Example("t", at(10), at(20))])
    ok("a test example predating the cutoff is caught",
       raises(lambda: splits.assert_no_leakage(early_test), SplitError))
    both = splits.Split(at(50), train=[Example("x", at(10), at(20))],
                        test=[Example("x", at(70), at(80))])
    ok("an example on both sides is caught",
       raises(lambda: splits.assert_no_leakage(both), SplitError))
    ok("a label predating its own forecast is refused at split time",
       raises(lambda: splits.split(
           [Example("impossible", at(20), at(10))], at(50)), SplitError))

    print("\n[6] Walk-forward folds do not accumulate\n")
    folds = splits.rolling_splits(pool, [at(30), at(50), at(60)],
                                  min_retention=0.0)
    ok("one split per cutoff", len(folds) == 3)
    ok("later folds train on more", len(folds[2].train) > len(folds[0].train),
       [len(f.train) for f in folds])
    ok("each is computed independently of the others",
       all(f.cutoff == c for f, c in zip(folds, [at(30), at(50), at(60)])))
    ok("duplicate cutoffs collapse",
       len(splits.rolling_splits(pool, [at(50), at(50)], min_retention=0.0)) == 1)
    print("        " + "  ".join(f"{f.cutoff[:10]}: train={len(f.train)}"
                                 for f in folds))

    print("\n[7] Built from the record's own two timestamps\n")
    con = ledger.connect(":memory:", create=True)
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from tests_support import build_two_forecasts  # noqa: E402
    build_two_forecasts(con)
    exs = splits.from_forecasts(con)
    ok("examples come straight from the forecasts table", len(exs) == 2)
    ok("a forecast with no label_available_at lands in unlabelled",
       len(splits.split(exs, at(400)).unlabelled) == 2, exs)
    ok("filtering by model version works",
       len(splits.from_forecasts(con, model_version="nope")) == 0)

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

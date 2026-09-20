"""
Temporal splits that do not leak (§8.3).

> "The fit/validation/test date split is insufficient alone for overlapping
> multi-year forecasts: an example created before a cutoff may resolve after it.
> Require `label_available_at` to precede the cutoff and purge overlapping
> examples. Fit normalisers, feature selection, source scores and calibration
> inside the same temporal discipline."

The ordinary split — train on everything before the cutoff, test on everything
after — is wrong here for a reason specific to forecasting: a forecast is made
at one time and *resolves* at another, so an example that sits on the training
side by creation date may only become labelled well after the cutoff. Including
it means fitting on a label that did not exist, which is leakage of the purest
kind: not a subtle correlation, an outright answer.

Three groups, and naming all three is the point:

  * **train** — created before the cutoff *and* labelled by it.
  * **purged** — created before the cutoff, labelled after. Not training data,
    because the label was not available; not test data either, because the
    model has seen the question. Dropped, and counted.
  * **test** — created after the cutoff, and after the embargo.

**The embargo exists because dropping the overlapping examples is not enough.**
An example created just after the cutoff was made under conditions the training
labels describe — the same week's news, the same regime — so it is not
independent of the training set even though its dates look clean. The embargo is
a gap, and it is a policy rather than a derivation.

The purge rate is reported rather than hidden. On long-horizon questions it can
be most of the sample, and a split that quietly discarded 80% of the data would
otherwise look like a split that worked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from . import timeutil

# How long after the cutoff before a new example counts as independent of the
# training labels. One week: long enough to clear a news cycle on the T1
# horizons this project starts with.
DEFAULT_EMBARGO_SECONDS = 7 * 86400.0

# A split that keeps less than this fraction of the pre-cutoff sample is
# reporting on something other than the data it was given.
MIN_RETENTION = 0.20


class SplitError(RuntimeError):
    """A split as requested would leak, or would not mean what it claims."""


@dataclass(frozen=True)
class Example:
    """Anything with a creation time and a moment its label became knowable."""
    key: str
    created_at: str
    label_available_at: str | None
    payload: object = None


@dataclass(frozen=True)
class Split:
    cutoff: str
    train: list[Example] = field(default_factory=list)
    test: list[Example] = field(default_factory=list)
    purged: list[Example] = field(default_factory=list)
    embargoed: list[Example] = field(default_factory=list)
    unlabelled: list[Example] = field(default_factory=list)

    @property
    def purge_rate(self) -> float:
        """Share of pre-cutoff examples dropped because their labels came late."""
        considered = len(self.train) + len(self.purged)
        return len(self.purged) / considered if considered else 0.0

    @property
    def retention(self) -> float:
        considered = len(self.train) + len(self.purged)
        return len(self.train) / considered if considered else 0.0

    def summary(self) -> str:
        return (f"train {len(self.train)}, test {len(self.test)}, "
                f"purged {len(self.purged)} ({self.purge_rate:.0%} of pre-cutoff), "
                f"embargoed {len(self.embargoed)}, "
                f"unlabelled {len(self.unlabelled)}")


def split(
    examples: list[Example],
    cutoff: str,
    *,
    embargo_seconds: float = DEFAULT_EMBARGO_SECONDS,
    min_retention: float = MIN_RETENTION,
) -> Split:
    """
    Partition examples around `cutoff`, purging the ones whose labels came late.

    Raises when retention falls below `min_retention`: at that point the fit is
    not being done on the data it appears to be done on, and reporting a
    training score for it would describe a sample chosen by resolution speed.
    Pass `min_retention=0.0` to allow it deliberately, which is reasonable when
    the question *is* how much the horizon costs.
    """
    if embargo_seconds < 0:
        raise SplitError("a negative embargo would put test data before the cutoff")
    cut = timeutil.canonical(cutoff)
    embargo_end = timeutil.iso(
        timeutil.parse(cut) + timedelta(seconds=embargo_seconds))

    train, test, purged, embargoed, unlabelled = [], [], [], [], []
    for ex in examples:
        created = timeutil.canonical(ex.created_at)
        if ex.label_available_at is None:
            unlabelled.append(ex)
            continue
        labelled = timeutil.canonical(ex.label_available_at)
        if labelled < created:
            raise SplitError(
                f"{ex.key!r} claims its label was available before it was "
                "created; a forecast cannot resolve before it exists")
        if created < cut:
            (train if labelled <= cut else purged).append(ex)
        elif created < embargo_end:
            embargoed.append(ex)
        else:
            test.append(ex)

    result = Split(cut, train, test, purged, embargoed, unlabelled)
    if min_retention > 0 and result.retention < min_retention and (train or purged):
        raise SplitError(
            f"only {result.retention:.0%} of pre-cutoff examples survive the "
            f"purge ({len(purged)} of {len(train) + len(purged)} resolve after "
            "the cutoff). Fitting on what is left describes a sample selected "
            "by how fast its questions resolved, not the question set. Move the "
            "cutoff later, or pass min_retention=0.0 if the horizon cost is "
            "what you are measuring")
    return result


def rolling_splits(
    examples: list[Example],
    cutoffs: list[str],
    **kw,
) -> list[Split]:
    """
    A split per cutoff, for walk-forward evaluation.

    Each is independent: nothing accumulates across them, because a normaliser
    or a source score carried forward from an earlier fold is fitted on data the
    later fold treats as unseen. §8.3's "inside the same temporal discipline"
    applies to every fitted quantity, not only to the model.
    """
    return [split(examples, c, **kw) for c in sorted(set(cutoffs))]


def assert_no_leakage(s: Split) -> None:
    """
    Verify a split after the fact. Cheap, and it catches a reordered filter.

    The properties are obvious and that is why they are worth asserting: the
    conditions that produce leakage are exactly the ones that look fine.
    """
    for ex in s.train:
        if timeutil.canonical(ex.label_available_at) > s.cutoff:
            raise SplitError(
                f"train example {ex.key!r} has a label that became available "
                f"after the cutoff: fitting on it uses an answer that did not "
                "exist")
        if timeutil.canonical(ex.created_at) >= s.cutoff:
            raise SplitError(f"train example {ex.key!r} was created after the cutoff")
    for ex in s.test:
        if timeutil.canonical(ex.created_at) < s.cutoff:
            raise SplitError(
                f"test example {ex.key!r} predates the cutoff and the model may "
                "have been fitted on it")
    train_keys = {e.key for e in s.train}
    overlap = train_keys & {e.key for e in s.test}
    if overlap:
        raise SplitError(f"{len(overlap)} example(s) appear in both sides: "
                         f"{sorted(overlap)[:3]}")


def from_forecasts(con, model_version: str | None = None) -> list[Example]:
    """
    Build examples from the record, using the two times the schema already keeps.

    `label_available_at` is nullable on `forecasts`, and a forecast without one
    lands in `unlabelled` rather than being assumed resolvable — the assumption
    would silently move unresolved questions into training.
    """
    sql = ("SELECT forecast_hash, created_at, label_available_at, p_est_bp "
           "FROM forecasts")
    args: list = []
    if model_version:
        sql += " WHERE model_version = ?"
        args.append(model_version)
    sql += " ORDER BY created_at"
    return [Example(key=h, created_at=c, label_available_at=la, payload=p)
            for h, c, la, p in con.execute(sql, args)]

"""
Ablations: which component actually contributes?

v2 §12 — "without these comparisons, you may build an elaborate pipeline without
learning which components actually contribute predictive information." That is
the failure mode this project is most exposed to, because every component in it
sounds reasonable.

The comparisons that matter:

  * Does adding verified signals beat the baseline?
  * Does deduplication help, or is raw article count as good?
  * Does lineage weighting help over naive source counting?
  * **Does the evidence model beat the contemporaneous market price?** — the only
    one whose answer determines whether the project has a point.

Two design choices, both consequential:

**Comparisons are paired.** Same contracts, same information cutoffs, matched by
key. Comparing two models on different question sets measures the questions, not
the models — and paired differences remove question difficulty, which is usually
the dominant variance term. Unpaired comparison is refused rather than warned
about.

**The regime bootstrap is reused unchanged.** Paired differences inherit the same
serial-dependence problem as raw scores (Phase 1), so the same twelve-regime
requirement applies. There is no separate, laxer path for ablations.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .scoring import (
    MIN_REGIMES,
    BootstrapResult,
    Observation,
    ScoringError,
    brier,
    murphy,
    regime_bootstrap_ci,
)


class AblationError(RuntimeError):
    """The comparison as requested would not be meaningful."""


@dataclass(frozen=True)
class Variant:
    """One model configuration and the forecasts it produced."""
    name: str
    observations: list[Observation]
    description: str = ""


@dataclass(frozen=True)
class Comparison:
    variant: str
    reference: str
    n_paired: int
    n_regimes: int
    mean_brier_variant: float
    mean_brier_reference: float
    delta_brier: float          # reference - variant; positive means variant is better
    ci: BootstrapResult
    reliability_variant: float
    reliability_reference: float

    @property
    def contributes(self) -> bool:
        """Whether the component earns its place: CI lower bound clears zero."""
        return self.ci.fires

    def verdict(self) -> str:
        if self.contributes:
            return f"CONTRIBUTES (delta {self.delta_brier:+.4f}, lower {self.ci.lower:+.4f})"
        if self.ci.upper < 0:
            return f"HARMS (delta {self.delta_brier:+.4f}, upper {self.ci.upper:+.4f})"
        return f"no detectable effect (delta {self.delta_brier:+.4f}, " \
               f"CI [{self.ci.lower:+.4f}, {self.ci.upper:+.4f}])"


def _align(variant: Variant, reference: Variant) -> list[tuple[Observation, Observation]]:
    """Match observations by key. Refuses anything that is not a clean pairing."""
    if not variant.observations or not reference.observations:
        raise AblationError("both variants need observations")

    v_keys = [o.key for o in variant.observations]
    r_keys = [o.key for o in reference.observations]
    if any(not k for k in v_keys + r_keys):
        raise AblationError(
            "every observation needs a key for paired comparison. Comparing on "
            "unkeyed data would measure the question set, not the models"
        )
    if len(set(v_keys)) != len(v_keys) or len(set(r_keys)) != len(r_keys):
        raise AblationError("duplicate keys within a variant; pairing is ambiguous")

    v_map = {o.key: o for o in variant.observations}
    r_map = {o.key: o for o in reference.observations}
    shared = sorted(set(v_map) & set(r_map))
    if not shared:
        raise AblationError("no shared keys; the variants were run on different questions")

    dropped = (len(v_map) - len(shared)) + (len(r_map) - len(shared))
    if dropped and len(shared) < 0.8 * max(len(v_map), len(r_map)):
        raise AblationError(
            f"only {len(shared)} of {max(len(v_map), len(r_map))} questions are shared. "
            "Comparing on a heavily filtered intersection selects on outcome"
        )

    pairs = []
    for k in shared:
        v, r = v_map[k], r_map[k]
        if v.outcome != r.outcome:
            raise AblationError(f"key {k!r} has different outcomes in the two variants")
        if v.regime_id != r.regime_id:
            raise AblationError(f"key {k!r} sits in different regimes in the two variants")
        pairs.append((v, r))
    return pairs


def compare(
    variant: Variant,
    reference: Variant,
    resamples: int = 2000,
    seed: int = 0,
    min_regimes: int = MIN_REGIMES,
) -> Comparison:
    """
    Paired comparison of `variant` against `reference` on shared questions.

    The bootstrap runs on synthetic observations whose `p_base` is the reference
    forecast, so the paired difference the CI covers is exactly
    `Brier(reference) − Brier(variant)` — positive when the variant is better.
    """
    pairs = _align(variant, reference)

    paired = [
        Observation(p=v.p, outcome=v.outcome, p_base=r.p,
                    regime_id=v.regime_id, key=v.key)
        for v, r in pairs
    ]
    ci = regime_bootstrap_ci(paired, resamples=resamples, seed=seed,
                             min_regimes=min_regimes)

    v_obs = [v for v, _ in pairs]
    r_obs = [r for _, r in pairs]
    return Comparison(
        variant=variant.name,
        reference=reference.name,
        n_paired=len(pairs),
        n_regimes=ci.n_regimes,
        mean_brier_variant=brier(v_obs),
        mean_brier_reference=brier(r_obs),
        delta_brier=brier(r_obs) - brier(v_obs),
        ci=ci,
        reliability_variant=murphy(v_obs)["reliability"],
        reliability_reference=murphy(r_obs)["reliability"],
    )


def ablate(
    full: Variant,
    ablated: list[Variant],
    resamples: int = 2000,
    seed: int = 0,
) -> list[Comparison]:
    """
    Compare the full model against each ablated version.

    Each comparison asks: does removing this component hurt? A component whose
    removal changes nothing is not contributing, however good its rationale.
    """
    return [compare(full, a, resamples=resamples, seed=seed) for a in ablated]


def beats_market(
    variant: Variant,
    market: Variant,
    resamples: int = 2000,
    seed: int = 0,
) -> Comparison:
    """
    The comparison that decides whether any of this has a point.

    Identical mechanics to `compare`, named separately because it is the one
    result that should never be buried in a table of seven others.
    """
    return compare(variant, market, resamples=resamples, seed=seed)


def report(comparisons: list[Comparison]) -> str:
    """Fixed-width summary. Reliability is shown alongside because skill is not calibration."""
    lines = [
        f"{'variant':<26}{'vs':<20}{'ΔBrier':>10}{'CI lower':>11}{'verdict':>12}",
        "-" * 79,
    ]
    for c in comparisons:
        verdict = ("contributes" if c.contributes
                   else ("harms" if c.ci.upper < 0 else "no effect"))
        lines.append(
            f"{c.variant[:25]:<26}{c.reference[:19]:<20}"
            f"{c.delta_brier:>+10.4f}{c.ci.lower:>+11.4f}{verdict:>12}"
        )
    lines.append("-" * 79)
    if comparisons:
        c = comparisons[0]
        lines.append(
            f"paired on {c.n_paired} questions across {c.n_regimes} regimes; "
            f"reliability {c.reliability_variant:.5f} vs {c.reliability_reference:.5f}"
        )
    return "\n".join(lines)

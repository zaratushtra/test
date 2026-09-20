"""
Every tunable number in one place, with how it was arrived at.

This project makes a hard distinction between what it measured and what it
decided, and then scatters both across a dozen modules as bare constants and
default arguments. A reader cannot tell, from `ASSUMED_CORRELATION = 0.30` and
`POWER_COEFFICIENT = 31.4` sitting in two files, that the first is a policy
somebody chose and the second falls out of an algebraic identity. Both look
equally like facts.

So each is registered with its **provenance**:

  * ``measured``  — came out of a simulation or an analysis in this repo, and
    the entry says which one. Changing it means re-running that.
  * ``derived``   — follows from a definition or an identity. Changing it means
    the definition changed.
  * ``declared``  — somebody chose it. It is not wrong, but nothing in this
    repository supports it, and the entry says what would replace it.
  * ``external``  — a fact about the world or a venue, with a date, because
    those expire.

`tests/test_params.py` asserts that every module-level constant in `spine/` is
registered, so a new magic number cannot arrive unannounced. That is the point:
the registry is worth little as documentation and quite a lot as a gate.

**A number checked somewhere else does not belong here.** `SCHEMA_VERSION` was
registered on the reasoning that the registry should cover every constant, and
promptly went stale on the next schema bump — two sources of truth for one value
is exactly the failure this module exists to prevent. It is exempt, and
`tests/test_docs.py` checks it against `PRAGMA user_version` where the real
comparison lives. The registry is for numbers that were *chosen*, not for
numbers that must agree with something.

`unsupported()` lists everything `declared`. It is the project's own list of
things it is currently taking on faith, and it should shrink as the record
accumulates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

MEASURED, DERIVED, DECLARED, EXTERNAL = "measured", "derived", "declared", "external"


@dataclass(frozen=True)
class Param:
    name: str
    value: object
    where: str            # module.CONSTANT or module.function(arg=)
    provenance: str
    justification: str
    replaced_by: str = ""  # what would turn a `declared` into a `measured`

    def __post_init__(self):
        if self.provenance not in (MEASURED, DERIVED, DECLARED, EXTERNAL):
            raise ValueError(f"unknown provenance {self.provenance!r}")
        if self.provenance == DECLARED and not self.replaced_by:
            raise ValueError(
                f"{self.name}: a declared parameter must say what would replace "
                "it, or it is indistinguishable from a measurement nobody made")

    def line(self) -> str:
        return f"{self.provenance:<9} {self.where:<46} {self.value!r:>12}  {self.name}"


PARAMS: list[Param] = [
    # ---------------------------------------------------------------- derived
    Param("power coefficient", 31.4, "phase0/screen_markets.POWER_COEFFICIENT",
          DERIVED,
          "(z_0.975 + z_0.80)^2 * 4 = (1.96 + 0.8416)^2 * 4. v1's 24.7 used a "
          "one-sided alpha=0.05 while the gate reads a 2.5th bootstrap "
          "percentile, so every projection was ~27% optimistic."),
    Param("two-sided 95% quantile", 1.959963985, "spine/evaluate.Z_95", DERIVED,
          "The divisor that turns a 95% interval width back into a standard "
          "error. Not a choice."),
    Param("syndication correlation", 1.0, "spine/evidence.SYNDICATION_CORRELATION",
          DERIVED,
          "Byte-identical artifacts are one artifact. Their errors are the same "
          "errors, by definition, not by estimate."),
    Param("basis-point epsilon", 1, "spine/models.EPS_BP", DERIVED,
          "One basis point, the smallest representable step off the open "
          "interval's endpoints. Certainty is not a forecast."),

    # --------------------------------------------------------------- measured
    Param("minimum regimes", 12, "spine/scoring.MIN_REGIMES", MEASURED,
          "phase1/serial_dependence.py: false-positive rate 43.5% at 1 regime, "
          "13.0% at 4, 4.0% at 12. Below twelve the interval is too narrow to "
          "mean anything, so the function raises rather than returning a number."),

    # --------------------------------------------------------------- declared
    Param("assumed source correlation", 0.30, "spine/evidence.ASSUMED_CORRELATION",
          DECLARED,
          "A pair with no recorded estimate is unknown, not independent. The "
          "value is chosen to be non-zero rather than to be correct — assuming "
          "independence is the flattering direction and must not be the default.",
          replaced_by="observed_error_agreement estimates, once enough resolved "
                      "cases exist to compute them (docs/EVIDENCE-REPORT.md §2.2)"),
    Param("assumed ownership correlation", 0.75,
          "spine/evidence.ASSUMED_OWNERSHIP_CORRELATION", DECLARED,
          "Shared ownership is a stronger clue than nothing and weaker than "
          "syndication. §7.2 is explicit that ownership is a clue, not a "
          "determination.",
          replaced_by="the same observed error agreement, which should make the "
                      "ownership prior unnecessary"),
    Param("reference source count", 3.0, "spine/evidence.N_REF_SOURCES", DECLARED,
          "The independence weight reaches 1.0 at three effectively independent "
          "sources. The bound matters more than the level: v1 let this exceed 1 "
          "and thereby broke its own influence cap.",
          replaced_by="a fit of contribution weight against realised predictive "
                      "value, which needs resolved forecasts"),
    Param("influence caps by verification state", "1.5 / 0.5 / 0.25",
          "spine/evidence.CAP_BY_ESTABLISHES", DECLARED,
          "How much a claim may move a forecast, by what it establishes. Caps "
          "exposure to a claim, never the claim's content — and deliberately "
          "never zero, since an announcement can BE the resolving fact (§7.1).",
          replaced_by="the levels at which capping stops improving out-of-sample "
                      "Brier, measurable once there is a record"),
    Param("authenticity cap factors", "1.0 / 0.5 / 0.25",
          "spine/evidence.CAP_FACTOR_BY_AUTHENTICITY", DECLARED,
          "An unverified artifact cannot carry a verified one's weight.",
          replaced_by="observed accuracy conditional on verification state"),
    Param("fidelity cap factors", "1.0 / 0.6 / 0.3",
          "spine/evidence.CAP_FACTOR_BY_FIDELITY", DECLARED,
          "A lossy extraction has lost something, and the loss is not random.",
          replaced_by="measured extraction-fidelity error rates"),
    Param("minimum edge", 100.0, "spine/decision.decide(min_edge_bp=)", DECLARED,
          "1c per share. An edge smaller than the noise in our own price "
          "estimate is not an edge.",
          replaced_by="the realised-cost spread from "
                      "shadow.adverse_selection_report(), which produces exactly "
                      "this number from recorded fills"),
    Param("interval widening per contribution", 25,
          "spine/models.independent_forecast(widen_per_contribution_bp=)",
          DECLARED,
          "Evidence moves the estimate and adds model risk. A forecast that grew "
          "more confident purely because more news arrived would be counting "
          "corroboration as certainty.",
          replaced_by="the observed spread of realised outcomes against interval "
                      "width, i.e. coverage"),
    Param("near-duplicate threshold", 0.60,
          "spine/evidence.cluster_items(duplicate_threshold=)", DECLARED,
          "5-shingle Jaccard above which two texts are the same story. "
          "Syndicated copies score ~1.0, so the threshold is not delicate.",
          replaced_by="a labelled sample of true syndication pairs"),
    Param("topic threshold", 0.35,
          "spine/evidence.cluster_items(topic_threshold=)", DECLARED,
          "Only used for UNANCHORED items, and known to under-merge: independent "
          "reports of one event measured 0.061 (docs/EVIDENCE-REPORT.md §3). "
          "Under-merging is the safe direction — evidence unused rather than "
          "miscounted.",
          replaced_by="nothing; the anchor replaces it. This is a fallback that "
                      "should see little use"),
    Param("clustering window", 72.0,
          "spine/evidence.cluster_items(window_hours=)", DECLARED,
          "How far apart two items can be and still concern one event.",
          replaced_by="the observed distribution of coverage lag per event family"),
    Param("verification lag", 600.0,
          "spine/collect.run_query(verification_lag_seconds=)", DECLARED,
          "Ten minutes from first sight to usable by a decision. A source where "
          "verification is genuinely instant — an exchange feed, a court docket "
          "— should say so explicitly rather than inherit this.",
          replaced_by="measured time from ingest to completed verification, per "
                      "source"),
    Param("book capture lag", 2.0,
          "spine/venue.snapshot_books(capture_lag_seconds=)", DECLARED,
          "How stale a book is by the time a decision could act on it.",
          replaced_by="measured round-trip latency from the venue"),
    Param("maximum feed size", 8 * 1024 * 1024, "spine/collect.MAX_FEED_BYTES",
          DECLARED,
          "A feed larger than this is not a feed we should be parsing. Paired "
          "with the DOCTYPE/ENTITY refusal, since entity expansion turns a few "
          "hundred bytes into gigabytes.",
          replaced_by="nothing; this is a safety limit, not an estimate"),

    Param("embargo", 7 * 86400.0, "spine/splits.DEFAULT_EMBARGO_SECONDS",
          DECLARED,
          "How long after a cutoff before a new example counts as independent "
          "of the training labels. Dropping the overlapping examples is not "
          "enough: one created just after the cutoff was made under conditions "
          "the training labels describe, so its dates look clean and it is not "
          "independent (§8.3).",
          replaced_by="the measured autocorrelation of forecast errors against "
                      "elapsed time, per event family"),
    Param("minimum retention", 0.20, "spine/splits.MIN_RETENTION", DECLARED,
          "Below this share surviving the purge, the fit describes a sample "
          "selected by how fast its questions resolved rather than the question "
          "set, so the split raises instead of reporting on what is left.",
          replaced_by="nothing directly; it is a guard. The right cutoff is a "
                      "study design decision the guard exists to force"),

    Param("maximum book age", 900.0, "spine/shadow.MAX_BOOK_AGE_SECONDS",
          DECLARED,
          "Past this a book is not a price, it is a memory. One ordinary "
          "collection cycle plus slack. book_at() used to return the newest "
          "book however old, so a collector that died on Friday left Monday "
          "pricing against Friday's market — the §11.1 failure exactly.",
          replaced_by="the measured distribution of price movement over elapsed "
                      "time, per event family: the age at which a book stops "
                      "predicting the current touch"),
    Param("lease duration", 900.0, "spine/lease.DEFAULT_TTL_SECONDS", DECLARED,
          "Long enough to survive an ordinary collection cycle, short enough "
          "that a dead collector stops mattering within one. §11.1: a lease "
          "fails safe on its own, where a flag needs something alive to clear "
          "it — and the failure that most needs clearing it is that thing "
          "dying.",
          replaced_by="the observed time between successful collection passes, "
                      "once there is a run long enough to have a distribution"),

    Param("exit band", 300, "spine/sizing.EXIT_BAND_BP", DECLARED,
          "How far from the touch depth still counts as exitable. Beyond this "
          "the book is not liquidity you can leave through in a hurry, it is "
          "liquidity you would move.",
          replaced_by="measured price impact of exits at various sizes, from "
                      "recorded books and shadow fills"),
    Param("depth share", 0.25, "spine/sizing.DEPTH_SHARE", DECLARED,
          "The share of exitable depth one position may take. Taking all of it "
          "means being the entire other side on the way out.",
          replaced_by="the same impact measurements; this is the level at which "
                      "exit cost stops being linear"),

    Param("safe url schemes", "http, https", "spine/untrusted.SAFE_URL_SCHEMES",
          DECLARED,
          "The only schemes that denote a document you could go and read. "
          "javascript:, data: and file: are instructions waiting to be "
          "followed, and a stored hazard is a hazard on the day something reads "
          "it rather than the day it arrives (§11.3).",
          replaced_by="nothing; this is a safety boundary, not an estimate. It "
                      "would widen only if a real source served articles over "
                      "another scheme"),

    # --------------------------------------------------------------- external
    Param("bidirectional controls", "13 codepoints",
          "spine/untrusted.BIDI_CONTROLS", EXTERNAL,
          "The Unicode bidi formatting characters, as of Unicode 15 (2026). "
          "They reorder rendered text without changing the bytes, so what is "
          "hashed and what is read differ — Trojan Source, CVE-2021-42574."),
    Param("zero-width characters", "4 codepoints", "spine/untrusted.ZERO_WIDTH",
          EXTERNAL,
          "ZWSP, ZWNJ, ZWJ and BOM as of Unicode 15 (2026): invisible, and they "
          "defeat naive equality and search."),
    Param("close-only jurisdictions", "GB, UK",
          "spine/registry.CLOSE_ONLY", EXTERNAL,
          "Verified 19 Sep 2026: Polymarket is close-only on both frontend and "
          "API in the UK. A venue geoblock policy, not an authority — re-check "
          "before relying on it."),
]

BY_WHERE = {p.where: p for p in PARAMS}


def unsupported() -> list[Param]:
    """
    Everything this project is currently taking on faith.

    Not a list of problems — a declared parameter is an honest thing to have.
    It is a list of the places where a number is doing work that no measurement
    in this repository supports, which is worth being able to read in one go.
    """
    return [p for p in PARAMS if p.provenance == DECLARED]


def report() -> str:
    lines = [f"{'provenance':<9} {'where':<46} {'value':>12}  name", "-" * 96]
    order = {DERIVED: 0, MEASURED: 1, EXTERNAL: 2, DECLARED: 3}
    for p in sorted(PARAMS, key=lambda q: (order[q.provenance], q.where)):
        lines.append(p.line())
    lines.append("-" * 96)
    n = len(unsupported())
    lines.append(f"{len(PARAMS)} parameters; {n} declared rather than measured "
                 f"({n / len(PARAMS):.0%})")
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
    print("\nTaken on faith:\n")
    for p in unsupported():
        print(f"  {p.where}\n      -> {p.replaced_by}\n")


# ---------------------------------------------------------------------------
# the governance record (§8.4)
# ---------------------------------------------------------------------------

def snapshot() -> dict:
    """
    Every registered parameter and its current value, canonically ordered.

    §8.4: "v1 named λ and λ_sig. Also tunable: reference-class selection,
    feature weights, ρ, clustering thresholds, source weights, market-selection
    rules. **All are researcher degrees of freedom and all belong in the
    governance record.**"

    Listing them in a module is not a governance record. A degree of freedom
    that is not recorded *at the moment a forecast was made* is one that can be
    adjusted afterwards, and the record will not show it — two forecasts made
    under different settings would be indistinguishable, which is precisely the
    freedom §8 exists to close.
    """
    return {
        "parameters": {
            p.where: {"value": p.value, "provenance": p.provenance}
            for p in sorted(PARAMS, key=lambda q: q.where)
        },
        "declared_count": len(unsupported()),
        "total_count": len(PARAMS),
    }


def commit(con: sqlite3.Connection, created_at: str) -> str:
    """
    Store the current parameter snapshot as a manifest and return its hash.

    Content-addressed, so the same settings store once and a changed setting
    produces a different hash — which is what makes "these two forecasts were
    made under the same configuration" a checkable statement rather than a
    recollection.
    """
    from .ledger import put_manifest
    return put_manifest(con, "model_config", snapshot(), created_at)


def committed_in(con: sqlite3.Connection, manifest_hash: str) -> str | None:
    """The parameter-snapshot hash an inputs manifest commits to, if any."""
    import json
    row = con.execute("SELECT content FROM manifests WHERE manifest_hash=?",
                      (manifest_hash,)).fetchone()
    if not row:
        return None
    try:
        content = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    ref = content.get("params") if isinstance(content, dict) else None
    return ref if isinstance(ref, str) else None


def matches_current(con: sqlite3.Connection, params_hash: str) -> bool:
    """Whether a stored snapshot is the configuration running right now."""
    from .canonical import content_hash
    return params_hash == content_hash(snapshot())

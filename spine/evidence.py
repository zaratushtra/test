"""
The evidence pipeline: signals in, contract effects out.

v2 §6 calls this "the missing core of v1", and until now it has been missing
from v2 as well — seven tables with no code behind them. This module is that
code, and it is organised around the four things that go wrong:

**Duplication is not deduplication.** Two outlets carrying the same wire story
are one piece of evidence, not two. Counting sources rewards syndication, which
is exactly backwards: the more widely a single origin is copied, the more
confident a naive counter becomes. Identical content across sources is detected
and recorded as `syndication` correlation 1.0, and near-copies are found by
shingle overlap.

**Co-publication is not error independence** (§7.2). `n_eff_sources` is computed
from an error-correlation matrix, not a headcount, and a pair with no recorded
estimate gets an *assumed* floor rather than zero. Assuming independence is the
flattering direction, and the flattering direction is the one that must never be
the default.

**Correlations are point-in-time.** `source_error_correlation` carries
`computed_at` in its primary key so a correlation learned in March cannot be
applied to a decision made in February. Retrieval honours that.

**The cap binds the product, not the input.** v1 claimed `L_max ≈ 1.5` limited a
cluster to 4.5:1 while the contribution was `λ_sig · w_c · LLR` with `w_c > 1`
whenever `n_eff > N_ref` — so the cap did not hold. Here the entire product is
formed first and clamped last, and both numbers are returned so the clamping is
visible rather than silent.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .canonical import content_hash

# A pair of sources with no recorded correlation estimate is NOT assumed
# independent. This floor is a declared policy, not a measurement, and every
# n_eff computation that leans on it says so in its detail.
ASSUMED_CORRELATION = 0.30

# Same media group, no better estimate: ownership is a clue, not a determination
# (§7.2), but it is a much stronger clue than nothing.
ASSUMED_OWNERSHIP_CORRELATION = 0.75

# Identical content under two mastheads is one artifact, whatever the bylines.
SYNDICATION_CORRELATION = 1.0

# The influence cap by what the claim actually establishes (§7.1). This caps
# *exposure to the claim*, never the claim's estimate — the same distinction
# §5.1 draws for probabilities. A claim that establishes only that someone said
# something is not worthless; it is bounded.
CAP_BY_ESTABLISHES = {
    "underlying_fact": 1.5,
    "only_that_it_was_asserted": 0.5,
    "indeterminate": 0.25,
}

# An unverified artifact cannot carry the weight of a verified one regardless of
# what it establishes. Applied multiplicatively to the cap above.
CAP_FACTOR_BY_AUTHENTICITY = {
    "artifact_verified": 1.0,
    "artifact_unverified": 0.5,
    "artifact_disputed": 0.25,
}

CAP_FACTOR_BY_FIDELITY = {
    "checked_faithful": 1.0,
    "unchecked": 0.6,
    "known_lossy": 0.3,
}

# Reference independence: the evidence weight is not scaled above 1.0 no matter
# how many independent sources agree. v1 let this exceed 1 and thereby broke its
# own cap; the cap now binds regardless, but the weight is bounded too.
N_REF_SOURCES = 3.0

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "at", "by",
    "with", "that", "this", "it", "is", "was", "are", "were", "be", "been",
    "has", "have", "had", "will", "would", "said", "says",
}


class EvidenceError(RuntimeError):
    """An evidence operation would have recorded something misleading."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


def _parse(ts: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceError(f"unparseable timestamp {ts!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z")


# ---------------------------------------------------------------------------
# sources
# ---------------------------------------------------------------------------

def ensure_source(
    con: sqlite3.Connection,
    name: str,
    kind: str,
    owner_group: str | None = None,
    homepage: str | None = None,
    now: str | None = None,
) -> int:
    """Register a source, returning its id. Idempotent on name."""
    row = con.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    cur = con.execute(
        "INSERT INTO sources(name, kind, owner_group, homepage, created_at) "
        "VALUES(?,?,?,?,?)",
        (name, kind, owner_group, homepage, now or _now()),
    )
    con.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# ingestion
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Ingested:
    item_id: int
    content_hash: str
    duplicate_of: list[int] = field(default_factory=list)  # same content, other sources
    note: str = ""


def ingest_item(
    con: sqlite3.Connection,
    source_id: int,
    body: str,
    *,
    first_seen_at: str,
    item_class: str,
    verification_lag_seconds: float = 0.0,
    available_for_decision_at: str | None = None,
    url: str | None = None,
    event_at: str | None = None,
    claimed_published_at: str | None = None,
    title: str | None = None,
    body_ref: str | None = None,
    now: str | None = None,
) -> Ingested:
    """
    Record one signal item and detect cross-source duplication.

    Five timestamps, of which exactly one governs retrieval (§8.1):
    `available_for_decision_at`, which is `first_seen_at` plus however long
    verification actually takes. Passing a lag of zero asserts that verification
    is instantaneous, which is a claim about the pipeline and almost never true;
    it is allowed because some items (an exchange feed, a court docket) really
    are usable on arrival, but it is never inferred.

    `claimed_published_at` is recorded and deliberately never used for
    retrieval: it is the source's assertion about itself.
    """
    if verification_lag_seconds < 0:
        raise EvidenceError("verification lag cannot be negative")
    seen = _parse(first_seen_at)
    avail = (_parse(available_for_decision_at) if available_for_decision_at
             else seen + timedelta(seconds=verification_lag_seconds))
    if avail < seen:
        raise EvidenceError(
            "available_for_decision_at precedes first_seen_at: an item cannot be "
            "usable by a decision before the collector saw it")

    h = content_hash({"body": body.strip()})

    existing = con.execute(
        "SELECT id FROM signal_items WHERE content_hash=? AND source_id=?",
        (h, source_id)).fetchone()
    if existing:
        return Ingested(existing[0], h, note="already ingested from this source")

    others = [r[0] for r in con.execute(
        "SELECT id FROM signal_items WHERE content_hash=? AND source_id<>?",
        (h, source_id)).fetchall()]

    cur = con.execute(
        """INSERT INTO signal_items
           (content_hash, source_id, url, event_at, claimed_published_at,
            first_seen_at, artifact_created_at, available_for_decision_at,
            title, body_ref, item_class)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (h, source_id, url, event_at, claimed_published_at, _iso(seen),
         now or _now(), _iso(avail), title, body_ref, item_class))
    item_id = cur.lastrowid
    con.commit()

    note = ""
    if others:
        # Byte-identical text under a second masthead is syndication, not
        # corroboration. Record the correlation now, at the time it was
        # observed, so a later decision sees it and an earlier one does not.
        n = record_syndication(con, item_id, others, computed_at=_iso(avail))
        note = (f"identical content already held from {len(others)} other source(s); "
                f"{n} syndication correlation(s) recorded")
    return Ingested(item_id, h, others, note)


def record_syndication(
    con: sqlite3.Connection,
    item_id: int,
    other_item_ids: list[int],
    computed_at: str,
) -> int:
    """Record correlation 1.0 between the sources of byte-identical artifacts."""
    src = con.execute("SELECT source_id FROM signal_items WHERE id=?",
                      (item_id,)).fetchone()
    if not src:
        raise EvidenceError(f"no signal item {item_id}")
    written = 0
    for other in other_item_ids:
        o = con.execute("SELECT source_id FROM signal_items WHERE id=?",
                        (other,)).fetchone()
        if not o or o[0] == src[0]:
            continue
        a, b = sorted((src[0], o[0]))
        con.execute(
            "INSERT OR IGNORE INTO source_error_correlation"
            "(source_a, source_b, correlation, basis, n_observations, computed_at) "
            "VALUES(?,?,?,'syndication',1,?)",
            (a, b, SYNDICATION_CORRELATION, computed_at))
        written += 1
    con.commit()
    return written


# ---------------------------------------------------------------------------
# near-duplicate detection and clustering
# ---------------------------------------------------------------------------

def shingles(text: str, k: int = 5) -> set[str]:
    """Word k-shingles, stopwords kept.

    Stopwords are deliberately *not* stripped here, unlike the market screen's
    tokeniser. Two rewrites of one story differ mostly in function words, so
    removing them makes distinct paraphrases look identical — the opposite of
    what near-duplicate detection needs.
    """
    words = re.findall(r"[a-z0-9']+", text.lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + k]) for i in range(len(words) - k + 1)}


def overlap(a: set[str], b: set[str]) -> float:
    """Jaccard overlap. 1.0 means the same text."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def topic_tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    cluster_version: int
    item_ids: list[int]
    originators: list[int]
    window_start: str
    window_end: str
    near_duplicate_pairs: int


def cluster_items(
    con: sqlite3.Connection,
    items: list[dict],
    *,
    cluster_version: int,
    window_hours: float = 72.0,
    duplicate_threshold: float = 0.60,
    topic_threshold: float = 0.35,
    now: str | None = None,
) -> list[Cluster]:
    """
    Group items into versioned event clusters and mark near-duplicates.

    `items` are dicts with `id`, `body`, `available_for_decision_at`, plus two
    optional fields: `anchor`, the registered proposition the item was retrieved
    *for*, and `attributes_to`, naming an upstream source.

    **Anchor first, text second — and the order is not an optimisation.**
    Measured on this project's own fixtures, two independent reports of one
    committee vote shared two content words out of thirty-five: Jaccard 0.06,
    containment 0.12, shingle overlap 0.00. No threshold separates that from
    unrelated text, because independent reporting of the same event *is*
    lexically unrelated — that is what makes it independent. Lexical similarity
    therefore finds **duplication, not events**, and it is used here for exactly
    that.

    Grouping by event is query-driven instead: items are retrieved *for* a
    registered proposition, so the anchor is known at ingest and never has to be
    inferred. Items with no anchor fall back to lexical linkage, which will
    under-merge; that is the safe direction, since an item wrongly left out of a
    cluster is evidence not used, while one wrongly merged in is evidence
    miscounted.

    Clustering is versioned because it is a *model output*: rerunning it with
    different thresholds must produce a new version rather than silently
    restating history, which is why `event_clusters` has a composite key.

    **Originators are named only from explicit attribution.** §7.2 rejects the
    publication-lag heuristic: a late report may be independent confirmation
    rather than a copy, so "earliest timestamp wins" would systematically
    misattribute exactly the cases that matter. When nothing in a cluster cites
    anything, the cluster has *no* originator and says so, rather than
    nominating the first arrival.
    """
    if not items:
        return []
    ts = now or _now()
    shingled = {it["id"]: shingles(it["body"]) for it in items}
    topics = {it["id"]: topic_tokens(it["body"]) for it in items}
    times = {it["id"]: _parse(it["available_for_decision_at"]) for it in items}

    parent = {it["id"]: it["id"] for it in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    anchors = {it["id"]: (it.get("anchor") or "") for it in items}
    dup_pairs = 0
    ids = [it["id"] for it in items]
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            if abs((times[a] - times[b]).total_seconds()) > window_hours * 3600:
                continue
            ov = overlap(shingled[a], shingled[b])
            if ov >= duplicate_threshold:
                dup_pairs += 1
                union(a, b)
            elif anchors[a] and anchors[a] == anchors[b]:
                union(a, b)
            elif not anchors[a] and not anchors[b] and \
                    overlap(topics[a], topics[b]) >= topic_threshold:
                union(a, b)

    by_root: dict[int, list[int]] = {}
    for i in ids:
        by_root.setdefault(find(i), []).append(i)

    attributions = {it["id"]: (it.get("attributes_to") or "") for it in items}
    names = {}
    for it in items:
        row = con.execute(
            "SELECT s.name FROM signal_items i JOIN sources s ON s.id=i.source_id "
            "WHERE i.id=?", (it["id"],)).fetchone()
        names[it["id"]] = row[0] if row else ""

    out = []
    for root, members in sorted(by_root.items()):
        cid = f"evc{root}"
        w_start = min(times[m] for m in members)
        w_end = max(times[m] for m in members)
        con.execute(
            "INSERT OR REPLACE INTO event_clusters"
            "(cluster_id, cluster_version, created_at, window_start, window_end) "
            "VALUES(?,?,?,?,?)",
            (cid, cluster_version, ts, _iso(w_start), _iso(w_end)))

        cited = {attributions[m] for m in members if attributions[m]}
        originators = [m for m in members
                       if cited and names[m] in cited and not attributions[m]]
        for m in members:
            con.execute(
                "INSERT OR REPLACE INTO cluster_members"
                "(cluster_id, cluster_version, item_id, is_originator) "
                "VALUES(?,?,?,?)",
                (cid, cluster_version, m, 1 if m in originators else 0))
        out.append(Cluster(cid, cluster_version, sorted(members),
                           sorted(originators), _iso(w_start), _iso(w_end),
                           dup_pairs))
    con.commit()
    return out


# ---------------------------------------------------------------------------
# effective sources
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EffectiveSources:
    n_sources: int
    n_eff: float
    mean_correlation: float
    assumed_pairs: int
    measured_pairs: int

    @property
    def leans_on_assumptions(self) -> bool:
        return self.assumed_pairs > self.measured_pairs

    def summary(self) -> str:
        return (f"{self.n_sources} sources -> n_eff {self.n_eff:.2f} "
                f"(r̄ {self.mean_correlation:.3f}; {self.measured_pairs} measured, "
                f"{self.assumed_pairs} assumed)")


def pairwise_correlation(
    con: sqlite3.Connection, a: int, b: int, as_of: str
) -> tuple[float, str]:
    """
    Error correlation between two sources *as known at `as_of`*.

    Point-in-time by construction: `computed_at <= as_of`. A correlation learned
    in March must not sharpen a decision made in February, and the primary key
    on `source_error_correlation` keeps every vintage so it cannot.

    Falls back, in order: recorded estimate, shared ownership, the assumed
    floor. It never falls back to zero — an unmeasured pair is unknown, not
    independent.
    """
    if a == b:
        return 1.0, "self"
    lo, hi = sorted((a, b))
    row = con.execute(
        "SELECT correlation, basis FROM source_error_correlation "
        "WHERE source_a=? AND source_b=? AND computed_at <= ? "
        "ORDER BY computed_at DESC LIMIT 1", (lo, hi, as_of)).fetchone()
    if row:
        return row[0], row[1]

    owners = con.execute(
        "SELECT id, owner_group FROM sources WHERE id IN (?,?)", (lo, hi)).fetchall()
    groups = {r[0]: r[1] for r in owners}
    if groups.get(lo) and groups.get(lo) == groups.get(hi):
        return ASSUMED_OWNERSHIP_CORRELATION, "ownership"
    return ASSUMED_CORRELATION, "assumed"


def n_eff_sources(
    con: sqlite3.Connection, source_ids: list[int], as_of: str
) -> EffectiveSources:
    """
    Kish effective sample size over the *error-correlation* matrix.

        n_eff = n^2 / (1'R1) = n / (1 + (n-1) r̄)

    with r̄ the mean off-diagonal correlation. Clamped to at most `n`: negative
    estimated correlations can push the formula above the headcount, which is
    mathematically fine and epistemically reckless — claiming more independent
    sources than sources is never something an estimate should be allowed to do.
    """
    uniq = sorted(set(source_ids))
    n = len(uniq)
    if n == 0:
        raise EvidenceError("no sources")
    if n == 1:
        return EffectiveSources(1, 1.0, 0.0, 0, 0)

    total = 0.0
    assumed = measured = 0
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            r, basis = pairwise_correlation(con, a, b, as_of)
            total += r
            if basis in ("assumed", "ownership"):
                assumed += 1
            else:
                measured += 1
    pairs = n * (n - 1) / 2
    r_bar = total / pairs
    denom = 1 + (n - 1) * r_bar
    n_eff = n / denom if denom > 0 else float(n)
    return EffectiveSources(n, min(float(n), max(1.0, n_eff)), r_bar,
                            assumed, measured)


# ---------------------------------------------------------------------------
# claims
# ---------------------------------------------------------------------------

def record_claim(
    con: sqlite3.Connection,
    *,
    cluster_id: str,
    cluster_version: int,
    assertion: str,
    authenticity: str,
    extraction_fidelity: str,
    establishes: str,
    source_ids: list[int],
    available_for_decision_at: str,
    feature_version: str,
    primary_artifact_id: int | None = None,
    now: str | None = None,
) -> tuple[int, EffectiveSources]:
    """
    Record a claim with the four verification questions answered separately.

    The claim carries **no likelihood ratio** — §6's central correction. The
    same fact moves different contracts differently, so weight belongs on the
    effect, not here.

    `n_eff_sources` is computed at the claim's own availability time, not now,
    so the number stored is the one a decision at that moment could have known.
    """
    if not source_ids:
        raise EvidenceError("a claim needs at least one source")
    eff = n_eff_sources(con, source_ids, available_for_decision_at)
    h = content_hash({
        "cluster_id": cluster_id, "cluster_version": cluster_version,
        "assertion": assertion, "feature_version": feature_version,
    })
    existing = con.execute("SELECT id FROM claims WHERE claim_hash=?", (h,)).fetchone()
    if existing:
        return existing[0], eff
    cur = con.execute(
        """INSERT INTO claims
           (claim_hash, cluster_id, cluster_version, assertion, authenticity,
            extraction_fidelity, establishes, primary_artifact_id, n_eff_sources,
            available_for_decision_at, computed_at, feature_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (h, cluster_id, cluster_version, assertion, authenticity,
         extraction_fidelity, establishes, primary_artifact_id, eff.n_eff,
         available_for_decision_at, now or _now(), feature_version))
    con.commit()
    return cur.lastrowid, eff


def record_contradiction(
    con: sqlite3.Connection,
    claim_a: int,
    claim_b: int,
    kind: str,
    detected_at: str,
) -> int:
    """
    Record that two claims conflict. Nothing is downweighted.

    §7.3: automatic downweighting on contradiction lets an unsupported denial
    dilute strong evidence, which is an attack surface — anyone able to publish
    a denial could suppress evidence they dislike. Both claims stand until a
    named adjudicator decides, and the decision is logged.
    """
    cur = con.execute(
        "INSERT INTO claim_contradictions(claim_a, claim_b, kind, detected_at) "
        "VALUES(?,?,?,?)", (claim_a, claim_b, kind, detected_at))
    con.commit()
    return cur.lastrowid


def adjudicate(
    con: sqlite3.Connection,
    contradiction_id: int,
    adjudication: str,
    adjudicator: str,
    adjudicated_at: str,
) -> None:
    """Resolve a contradiction. Requires a named adjudicator and a reason."""
    if not adjudication.strip() or not adjudicator.strip():
        raise EvidenceError("an adjudication needs both a reason and a named adjudicator")
    n = con.execute(
        "UPDATE claim_contradictions SET adjudication=?, adjudicator=?, "
        "adjudicated_at=? WHERE id=?",
        (adjudication, adjudicator, adjudicated_at, contradiction_id)).rowcount
    if not n:
        raise EvidenceError(f"no contradiction {contradiction_id}")
    con.commit()


def open_contradictions(con: sqlite3.Connection, as_of: str) -> list[dict]:
    """Contradictions detected by `as_of` and not yet adjudicated."""
    cur = con.execute(
        "SELECT id, claim_a, claim_b, kind, detected_at FROM claim_contradictions "
        "WHERE detected_at <= ? AND adjudicated_at IS NULL ORDER BY id", (as_of,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# the influence budget
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Contribution:
    llr: float
    weight: float
    lambda_sig: float
    raw: float           # the full product, before the cap
    final: float         # what actually enters the forecast
    cap: float

    @property
    def capped(self) -> bool:
        return abs(self.raw) > self.cap + 1e-12

    def summary(self) -> str:
        s = (f"llr {self.llr:+.3f} x w {self.weight:.3f} x lambda {self.lambda_sig:.2f} "
             f"= {self.raw:+.3f}")
        return s + (f" -> CAPPED at {self.final:+.3f}" if self.capped
                    else f" -> {self.final:+.3f}")


def evidence_weight(n_eff: float, n_ref: float = N_REF_SOURCES) -> float:
    """
    Independence weight, bounded at 1.0.

    v1 let this exceed 1 whenever `n_eff > N_ref`, which is how its cap stopped
    binding: the cap was checked against the LLR while the contribution was the
    scaled product. Bounding the weight is belt; clamping the product in
    `contribution()` is braces. Both, because this is the failure that made v1's
    stated 4.5:1 limit fictional.
    """
    if n_eff <= 0:
        raise EvidenceError("n_eff must be positive")
    return min(1.0, math.sqrt(n_eff / n_ref))


def cap_for(establishes: str, authenticity: str, extraction_fidelity: str) -> float:
    """
    The influence cap for a claim in a given verification state.

    This caps *exposure to the claim*, never the claim's content — the same
    move §5.1 makes for probabilities. Note what it does not do: it does not
    zero a claim that establishes only that something was asserted. Where a
    contract resolves on whether an authority published an announcement, the
    announcement *is* the fact (§7.1), and a blanket zero would discard exactly
    those cases. Bounded, not silenced.
    """
    if establishes not in CAP_BY_ESTABLISHES:
        raise EvidenceError(f"unknown establishes value {establishes!r}")
    if authenticity not in CAP_FACTOR_BY_AUTHENTICITY:
        raise EvidenceError(f"unknown authenticity value {authenticity!r}")
    if extraction_fidelity not in CAP_FACTOR_BY_FIDELITY:
        raise EvidenceError(f"unknown extraction_fidelity value {extraction_fidelity!r}")
    return (CAP_BY_ESTABLISHES[establishes]
            * CAP_FACTOR_BY_AUTHENTICITY[authenticity]
            * CAP_FACTOR_BY_FIDELITY[extraction_fidelity])


def contribution(
    llr: float, n_eff: float, cap: float, lambda_sig: float = 1.0,
    n_ref: float = N_REF_SOURCES,
) -> Contribution:
    """
    Form the whole product, then clamp. In that order, always.

    The v1 bug in one line: it clamped `llr` and then multiplied. Every scaling
    factor must be inside the cap or the cap is decoration.
    """
    if cap <= 0:
        raise EvidenceError("contribution cap must be positive")
    if lambda_sig < 0:
        raise EvidenceError("lambda_sig cannot be negative")
    w = evidence_weight(n_eff, n_ref)
    raw = llr * w * lambda_sig
    final = max(-cap, min(cap, raw))
    return Contribution(llr, w, lambda_sig, raw, final, cap)


def record_effect(
    con: sqlite3.Connection,
    *,
    claim_id: int,
    contract_id: int,
    horizon_days: float,
    llr: float,
    estimator: str,
    model_version: str,
    conditioned_on_ref: str,
    available_for_decision_at: str,
    lambda_sig: float = 1.0,
    now: str | None = None,
) -> tuple[int, Contribution]:
    """
    Record what a claim does to one contract over one horizon.

    The cap is derived from the claim's own verification state rather than
    passed in, so a caller cannot widen a weak claim's influence by asking for a
    larger budget.
    """
    row = con.execute(
        "SELECT n_eff_sources, establishes, authenticity, extraction_fidelity, "
        "       available_for_decision_at FROM claims WHERE id=?",
        (claim_id,)).fetchone()
    if not row:
        raise EvidenceError(f"no claim {claim_id}")
    n_eff, establishes, auth, fidelity, claim_avail = row

    if _parse(available_for_decision_at) < _parse(claim_avail):
        raise EvidenceError(
            "an effect cannot become available before the claim it is derived "
            "from: that would let a decision use evidence it could not have had")

    cap = cap_for(establishes, auth, fidelity)
    c = contribution(llr, n_eff, cap, lambda_sig)
    cur = con.execute(
        """INSERT INTO claim_contract_effects
           (claim_id, contract_id, horizon_days, llr, conditioned_on_ref,
            estimator, model_version, final_contribution, contribution_cap,
            available_for_decision_at, computed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (claim_id, contract_id, horizon_days, llr, conditioned_on_ref, estimator,
         model_version, c.final, cap, available_for_decision_at, now or _now()))
    con.commit()
    return cur.lastrowid, c


def usable_contributions(
    con: sqlite3.Connection,
    contract_id: int,
    as_of: str,
    model_version: str,
    horizon_days: float,
    include_unvalidated: bool = False,
) -> list[float]:
    """
    Contributions a forecast made at `as_of` may use, ready for §5.2's update.

    **Horizon-scoped, and it has to be.** An effect is defined per (claim,
    contract, horizon, model) because one fact moves "floor vote this month" and
    "law this year" by different amounts (§6). Retrieval that ignored the
    horizon would sum a claim's fourteen-day and one-year effects into a single
    forecast, double-counting the claim and mixing two incompatible questions.

    `llm_proposed_unvalidated` effects are excluded unless asked for explicitly.
    §6 is blunt about why: "an LLM-generated likelihood ratio of 3.2 is not a
    measurement". The schema types it so it cannot silently become an input;
    this is where that typing is enforced rather than merely available.

    One contribution per claim: the newest `computed_at` at or before `as_of`
    wins. Summing every vintage of a re-estimated effect would let a claim
    influence the forecast once per rerun.
    """
    estimators = ("fitted_model", "elicited_prior")
    if include_unvalidated:
        estimators = (*estimators, "llm_proposed_unvalidated")
    placeholders = ",".join("?" * len(estimators))
    cur = con.execute(
        f"""SELECT final_contribution FROM claim_contract_effects e
            WHERE contract_id = ? AND model_version = ? AND horizon_days = ?
              AND available_for_decision_at <= ?
              AND estimator IN ({placeholders})
              AND computed_at = (
                  SELECT MAX(computed_at) FROM claim_contract_effects
                  WHERE claim_id = e.claim_id AND contract_id = e.contract_id
                    AND model_version = e.model_version
                    AND horizon_days = e.horizon_days
                    AND available_for_decision_at <= ?)
            ORDER BY e.claim_id""",
        (contract_id, model_version, horizon_days, as_of, *estimators, as_of))
    return [r[0] for r in cur.fetchall()]

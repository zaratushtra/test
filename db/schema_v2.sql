-- ============================================================================
-- SPINE schema v2
--
-- Rewritten after the 19 Sep 2026 design review. Every table is STRICT, which
-- means timestamps are TEXT (ISO-8601 UTC, millisecond precision) — STRICT
-- permits only INT/INTEGER/REAL/TEXT/BLOB/ANY, so DATE and TIMESTAMP affinities
-- are gone deliberately, not by oversight.
--
-- What changed and why:
--   * The probability band is REMOVED. It confused P(event) with confidence in
--     the estimate and made the database refuse legitimate forecasts. Estimate,
--     uncertainty and trading permission are now three separate things.
--   * Likelihood ratios are bound to (claim, contract, horizon, model version).
--     A fact has no context-free LLR.
--   * The research proposition and the tradeable contract are separate objects.
--   * Settlement payout is recorded separately from the binary research outcome,
--     so a UMA 50/50 resolution cannot vanish from economic evaluation.
--   * Five distinct times are tracked; available_for_decision_at governs
--     retrieval, not arrival time.
--   * Reference classes are immutable versions, not mutable rows.
--   * Cycle detection is enforced, not asserted.
--   * The `sources` table exists.
--
-- Verified against SQLite 3.45.1. Probe suite: db/test_schema_v2.py
-- ============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 5000;

-- Schema version. spine/ledger.connect() REFUSES to open a database stamped
-- with a different one, rather than running new code against old tables --
-- which is how a query silently returns nothing and the absence gets read as
-- evidence. Bump this whenever this file changes in a way that is not purely
-- additive; ledger.SCHEMA_VERSION must match.
PRAGMA user_version = 8;

-- ============================================================================
-- CANONICAL TIMESTAMPS
--
-- Every point-in-time query here is a STRING comparison. That is correct for
-- exactly as long as every timestamp has the same shape -- and it did not:
-- datetime.isoformat() drops the fractional part on a whole second, so a row
-- written at 13:00:00Z compares GREATER than a cutoff of 13:00:00.000Z, because
-- '.' (0x2E) sorts before 'Z' (0x5A). A forecast recorded at an instant was
-- invisible to a retrieval at that same instant.
--
-- So every column that gets compared carries a GLOB check. Timestamps that are
-- merely recorded -- a venue's or a source's own claim about itself -- are left
-- free: those are evidence about the outside world, and forcing them into our
-- shape would be rewriting what the source said.
--
--   canonical form: YYYY-MM-DDTHH:MM:SS.sssZ  (UTC, milliseconds, always Z)
-- ============================================================================


-- ============================================================================
-- 1. EVIDENCE LAYER
-- ============================================================================

CREATE TABLE sources (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    kind               TEXT NOT NULL CHECK (kind IN
                         ('wire','outlet','aggregator','official','court','regulator',
                          'exchange','research','social','other')),
    owner_group        TEXT,              -- media group, for ownership collapse
    homepage           TEXT,
    created_at         TEXT NOT NULL
) STRICT;

-- Error-correlation between sources. Deliberately NOT named "co_publication":
-- the review is right that covering the same events is not the same as having
-- correlated errors. Co-publication is one input to the estimate, not the
-- estimate itself.
CREATE TABLE source_error_correlation (
    source_a           INTEGER NOT NULL REFERENCES sources(id),
    source_b           INTEGER NOT NULL REFERENCES sources(id),
    correlation        REAL NOT NULL CHECK (correlation >= -1.0 AND correlation <= 1.0),
    basis              TEXT NOT NULL CHECK (basis IN
                         ('shared_upstream','ownership','syndication','co_publication',
                          'observed_error_agreement','assumed')),
    n_observations     INTEGER NOT NULL CHECK (n_observations >= 0),
    computed_at            TEXT NOT NULL CHECK (computed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- point-in-time: never applied retroactively
    PRIMARY KEY (source_a, source_b, computed_at),
    CHECK (source_a < source_b)
) STRICT;

CREATE TABLE signal_items (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    content_hash             TEXT NOT NULL,
    source_id                INTEGER NOT NULL REFERENCES sources(id),
    url                      TEXT,
    -- The five times. Only the last one governs retrieval.
    event_at                 TEXT,        -- when the underlying event occurred
    claimed_published_at     TEXT,        -- the source's own claim; a claim, not a fact
    first_seen_at          TEXT NOT NULL CHECK (first_seen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- our collector observed it
    artifact_created_at      TEXT NOT NULL,   -- this row/version was created
    available_for_decision_at TEXT NOT NULL CHECK (available_for_decision_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- usable by a decision at or after this
    title                    TEXT,
    body_ref                 TEXT,
    -- Source text is preserved verbatim and flagged, never sanitised: if a
    -- publisher really emitted a bidirectional override, that is a fact about
    -- the publisher, and stripping it destroys evidence. NULL means clean.
    -- See spine/untrusted.py and design section 11.3.
    display_warnings         TEXT,
    -- A url the venue supplied that this project will not store as a reference:
    -- javascript:, data:, file:. The article is still evidence; the link is an
    -- instruction waiting for something to follow it.
    rejected_url_reason      TEXT,
    item_class               TEXT NOT NULL CHECK (item_class IN
                               ('reportage','opinion','market_commentary',
                                'primary_source','other')),
    UNIQUE (content_hash, source_id),
    CHECK (available_for_decision_at >= first_seen_at)
) STRICT;

CREATE INDEX idx_items_available ON signal_items (available_for_decision_at);

CREATE TABLE event_clusters (
    cluster_id         TEXT NOT NULL,
    cluster_version    INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    window_start       TEXT NOT NULL,
    window_end         TEXT NOT NULL,
    PRIMARY KEY (cluster_id, cluster_version)
) STRICT;

CREATE TABLE cluster_members (
    cluster_id         TEXT NOT NULL,
    cluster_version    INTEGER NOT NULL,
    item_id            INTEGER NOT NULL REFERENCES signal_items(id),
    is_originator      INTEGER NOT NULL DEFAULT 0 CHECK (is_originator IN (0,1)),
    PRIMARY KEY (cluster_id, cluster_version, item_id),
    FOREIGN KEY (cluster_id, cluster_version)
        REFERENCES event_clusters(cluster_id, cluster_version)
) STRICT;

-- A claim is what was asserted. It carries NO likelihood ratio: the same fact
-- has different predictive weight for different contracts.
CREATE TABLE claims (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_hash         TEXT NOT NULL UNIQUE,
    cluster_id         TEXT NOT NULL,
    cluster_version    INTEGER NOT NULL,
    assertion          TEXT NOT NULL,
    -- The four verification questions, answered separately.
    authenticity       TEXT NOT NULL CHECK (authenticity IN
                         ('artifact_verified','artifact_unverified','artifact_disputed')),
    extraction_fidelity TEXT NOT NULL CHECK (extraction_fidelity IN
                         ('checked_faithful','unchecked','known_lossy')),
    establishes        TEXT NOT NULL CHECK (establishes IN
                         ('underlying_fact','only_that_it_was_asserted','indeterminate')),
    primary_artifact_id INTEGER REFERENCES signal_items(id),
    n_eff_sources      REAL NOT NULL CHECK (n_eff_sources > 0),
    available_for_decision_at TEXT NOT NULL CHECK (available_for_decision_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    computed_at            TEXT NOT NULL CHECK (computed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    feature_version    TEXT NOT NULL,
    FOREIGN KEY (cluster_id, cluster_version)
        REFERENCES event_clusters(cluster_id, cluster_version)
) STRICT;

CREATE TRIGGER claims_append_only BEFORE UPDATE ON claims
BEGIN SELECT RAISE(ABORT, 'claims are append-only; emit a new feature_version'); END;

-- Contradictions are recorded, not auto-applied. An unsupported denial must not
-- dilute strong evidence.
CREATE TABLE claim_contradictions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_a            INTEGER NOT NULL REFERENCES claims(id),
    claim_b            INTEGER NOT NULL REFERENCES claims(id),
    kind               TEXT NOT NULL CHECK (kind IN
                         ('genuine_contradiction','changed_circumstances',
                          'scope_mismatch','unresolved')),
    adjudication       TEXT,
    adjudicator        TEXT,
    adjudicated_at     TEXT,
    detected_at            TEXT NOT NULL CHECK (detected_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    CHECK (claim_a <> claim_b)
) STRICT;

-- ============================================================================
-- 2. TARGET LAYER — research proposition vs tradeable contract
-- ============================================================================

CREATE TABLE propositions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposition_hash   TEXT NOT NULL UNIQUE,
    statement          TEXT NOT NULL,
    resolution_criterion TEXT NOT NULL,
    deadline_utc       TEXT NOT NULL,
    event_family       TEXT NOT NULL,
    horizon_class      TEXT NOT NULL CHECK (horizon_class IN ('T1','T2','T3')),
    created_at         TEXT NOT NULL
) STRICT;

CREATE TABLE contracts (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    venue                 TEXT NOT NULL,
    market_id             TEXT NOT NULL,
    outcome_token_id      TEXT NOT NULL,
    rules_text            TEXT NOT NULL,
    rules_version_hash    TEXT NOT NULL,
    deadline_utc          TEXT NOT NULL,
    resolution_source     TEXT NOT NULL,
    -- Payout is not binary: UMA can settle "Unknown" at 0.50.
    payout_states         TEXT NOT NULL,   -- JSON: {"YES":1.0,"NO":0.0,"UNKNOWN":0.5}
    fee_schedule_ref      TEXT,            -- fees vary by category; never assume
    eligibility_status    TEXT NOT NULL CHECK (eligibility_status IN
                            ('tradeable','close_only','blocked','unknown')),
    eligibility_checked_at TEXT NOT NULL,
    first_seen_at         TEXT NOT NULL,
    UNIQUE (venue, market_id, outcome_token_id, rules_version_hash)
) STRICT;

-- Binding a proposition to a contract is an explicit, reviewable act.
CREATE TABLE proposition_contract_binding (
    proposition_id     INTEGER NOT NULL REFERENCES propositions(id),
    contract_id        INTEGER NOT NULL REFERENCES contracts(id),
    match_quality      TEXT NOT NULL CHECK (match_quality IN
                         ('exact','material_mismatch','minor_divergence','unreviewed')),
    reviewer           TEXT NOT NULL,
    reviewed_at        TEXT NOT NULL,
    note               TEXT,
    PRIMARY KEY (proposition_id, contract_id)
) STRICT;

-- ============================================================================
-- 3. REFERENCE CLASSES — immutable versions
-- ============================================================================

CREATE TABLE reference_class_versions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    class_name         TEXT NOT NULL,
    version            INTEGER NOT NULL,
    description        TEXT NOT NULL,
    n                  INTEGER NOT NULL CHECK (n >= 0),
    k                  INTEGER NOT NULL CHECK (k >= 0 AND k <= n),
    -- Per-family prior. Beta(1,3) has mean 0.25 and is NOT a rare-event default.
    alpha              REAL NOT NULL CHECK (alpha > 0),
    beta               REAL NOT NULL CHECK (beta > 0),
    prior_justification TEXT NOT NULL,
    -- Exposure semantics: "X happens by D" needs a denominator, not just events.
    --
    -- exposure_units is THE DENOMINATOR the hazard divides by -- events per
    -- country-year, per case-week, whatever the family's natural unit is. It
    -- lived only in the Python dataclass and was never stored, which meant a
    -- deadline-aware forecast could not be reconstructed from the record: the
    -- number that produced it was not in it. Reconstruction would silently fall
    -- back to the static rate, the exact estimator section 5.3 rejected.
    exposure_units     REAL NOT NULL CHECK (exposure_units > 0.0),
    exposure_unit_name TEXT NOT NULL,   -- what one unit IS; no default, because
                                        -- guessing it is how 40 case-weeks becomes
                                        -- 40 case-years a year later
    exposure_window_days INTEGER,
    censoring_note     TEXT,
    frozen_at              TEXT NOT NULL CHECK (frozen_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    selection_rule_ref TEXT NOT NULL,   -- predeclared rule; a timestamp alone proves nothing
    UNIQUE (class_name, version)
) STRICT;

CREATE TRIGGER refclass_immutable_update BEFORE UPDATE ON reference_class_versions
BEGIN SELECT RAISE(ABORT, 'reference class versions are immutable; create a new version'); END;

CREATE TRIGGER refclass_immutable_delete BEFORE DELETE ON reference_class_versions
BEGIN SELECT RAISE(ABORT, 'reference class versions are immutable'); END;

CREATE TABLE reference_class_members (
    class_version_id   INTEGER NOT NULL REFERENCES reference_class_versions(id),
    event_name         TEXT NOT NULL,
    event_date         TEXT NOT NULL,    -- when it occurred
    outcome            INTEGER NOT NULL CHECK (outcome IN (0,1)),
    added_at           TEXT NOT NULL,    -- audit: when it entered the class
    source_url         TEXT,
    PRIMARY KEY (class_version_id, event_name)
) STRICT;

CREATE TRIGGER refclass_members_immutable BEFORE UPDATE ON reference_class_members
BEGIN SELECT RAISE(ABORT, 'class membership is immutable within a version'); END;

-- ============================================================================
-- 4. EVIDENCE -> CONTRACT EFFECTS
-- The missing core. An LLR is meaningless without a target and a horizon.
-- ============================================================================

CREATE TABLE claim_contract_effects (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id            INTEGER NOT NULL REFERENCES claims(id),
    contract_id         INTEGER NOT NULL REFERENCES contracts(id),
    horizon_days        REAL NOT NULL CHECK (horizon_days > 0),
    -- log P(E|Y=1,I) - log P(E|Y=0,I): conditional on information already in.
    llr                 REAL NOT NULL,
    conditioned_on_ref  TEXT NOT NULL,   -- manifest of I(t-) at estimation time
    estimator           TEXT NOT NULL CHECK (estimator IN
                          ('fitted_model','elicited_prior','llm_proposed_unvalidated')),
    model_version       TEXT NOT NULL,
    -- The influence budget binds the FINAL contribution, not the raw llr.
    final_contribution  REAL NOT NULL,
    contribution_cap    REAL NOT NULL CHECK (contribution_cap > 0),
    available_for_decision_at TEXT NOT NULL CHECK (available_for_decision_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    computed_at            TEXT NOT NULL CHECK (computed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    CHECK (abs(final_contribution) <= contribution_cap),
    -- horizon_days belongs in the key: one fact moves "floor vote this month"
    -- and "law this year" by different amounts (v2 section 6), so the two are
    -- separate effects, not a collision. So does estimator: an LLM's proposed
    -- ratio and a fitted estimate of the same quantity are different objects,
    -- and holding both side by side is how the proposal gets checked rather
    -- than trusted.
    UNIQUE (claim_id, contract_id, horizon_days, estimator, model_version, computed_at)
) STRICT;

CREATE TRIGGER effects_append_only BEFORE UPDATE ON claim_contract_effects
BEGIN SELECT RAISE(ABORT, 'effects are append-only; emit a new model_version'); END;

-- ============================================================================
-- 5. INPUT COMMITMENT — content-addressed, not label-addressed
-- ============================================================================

CREATE TABLE manifests (
    manifest_hash      TEXT PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN
                         ('forecast_inputs','model_config','prompt_bundle','data_snapshot')),
    content            TEXT NOT NULL,    -- canonical JSON enumerating every input by hash
    created_at         TEXT NOT NULL
) STRICT;

CREATE TRIGGER manifests_immutable BEFORE UPDATE ON manifests
BEGIN SELECT RAISE(ABORT, 'manifests are content-addressed and immutable'); END;

-- ============================================================================
-- 6. FORECASTS — estimate and uncertainty, no probability band
-- ============================================================================

CREATE TABLE forecasts (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_hash          TEXT NOT NULL UNIQUE,
    prev_hash              TEXT NOT NULL UNIQUE,
    proposition_id         INTEGER NOT NULL REFERENCES propositions(id),
    contract_id            INTEGER REFERENCES contracts(id),   -- null for research-only

    -- The estimate. Full open interval: a 2% ten-day forecast is legitimate.
    p_est_bp               INTEGER NOT NULL CHECK (p_est_bp > 0 AND p_est_bp < 10000),
    -- Uncertainty about the estimate, stated separately and never conflated.
    p_lo_bp                INTEGER NOT NULL CHECK (p_lo_bp > 0 AND p_lo_bp < 10000),
    p_hi_bp                INTEGER NOT NULL CHECK (p_hi_bp > 0 AND p_hi_bp < 10000),
    uncertainty_method     TEXT NOT NULL,
    -- Horizon class sets a MINIMUM interval width, never a cap on the estimate.
    min_width_bp           INTEGER NOT NULL CHECK (min_width_bp >= 0),

    p_base_bp              INTEGER NOT NULL CHECK (p_base_bp > 0 AND p_base_bp < 10000),
    p_market_bp            INTEGER CHECK (p_market_bp IS NULL OR
                                          (p_market_bp > 0 AND p_market_bp < 10000)),
    forecast_kind          TEXT NOT NULL CHECK (forecast_kind IN
                             ('independent','market_conditioned')),

    reference_class_version_id INTEGER NOT NULL REFERENCES reference_class_versions(id),
    inputs_manifest_hash   TEXT NOT NULL REFERENCES manifests(manifest_hash),
    model_version          TEXT NOT NULL,

    -- The unit across which the shared component g varies (v2 section 3.2).
    -- Phase 1 established that a week-level bootstrap cannot see g and reports
    -- intervals up to 10x too narrow; scoring MUST group by this.
    regime_id              TEXT NOT NULL,

    created_at             TEXT NOT NULL CHECK (created_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    label_available_at     TEXT CHECK (label_available_at IS NULL OR label_available_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),   -- when the outcome became knowable
    source                 TEXT NOT NULL CHECK (source IN ('human','llm_assisted','model')),
    source_detail          TEXT,

    CHECK (p_lo_bp <= p_est_bp AND p_est_bp <= p_hi_bp),
    CHECK (p_hi_bp - p_lo_bp >= min_width_bp)
) STRICT;

CREATE TRIGGER forecasts_no_update BEFORE UPDATE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are immutable after registration'); END;

CREATE TRIGGER forecasts_no_delete BEFORE DELETE ON forecasts
BEGIN SELECT RAISE(ABORT, 'forecasts are never deleted'); END;

CREATE TRIGGER forecasts_chain_head BEFORE INSERT ON forecasts
FOR EACH ROW WHEN (SELECT COUNT(*) FROM forecasts) > 0
BEGIN
    SELECT RAISE(ABORT, 'prev_hash does not match current chain head')
    WHERE NEW.prev_hash <> (SELECT forecast_hash FROM forecasts ORDER BY id DESC LIMIT 1);
END;

CREATE TRIGGER forecasts_chain_genesis BEFORE INSERT ON forecasts
FOR EACH ROW WHEN (SELECT COUNT(*) FROM forecasts) = 0
BEGIN
    SELECT RAISE(ABORT, 'genesis must use the genesis sentinel')
    WHERE NEW.prev_hash <> '0000000000000000000000000000000000000000000000000000000000000000';
END;

CREATE TRIGGER forecasts_class_frozen_first BEFORE INSERT ON forecasts
FOR EACH ROW
BEGIN
    SELECT RAISE(ABORT, 'reference class was not frozen before forecast creation')
    WHERE (SELECT frozen_at FROM reference_class_versions
           WHERE id = NEW.reference_class_version_id) >= NEW.created_at;
END;

-- A hash chain proves internal consistency. It does NOT prove when the chain
-- was created — a whole chain can be fabricated later with backdated stamps.
-- External anchoring is what makes pre-registration a claim anyone need believe.
CREATE TABLE chain_anchors (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_head_hash    TEXT NOT NULL,
    method             TEXT NOT NULL CHECK (method IN
                         ('rfc3161','public_append_only_log','blockchain','other')),
    external_ref       TEXT NOT NULL,
    proof              TEXT NOT NULL,
    anchored_at        TEXT NOT NULL
) STRICT;

CREATE TABLE forecast_edges (
    parent_hash        TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    child_hash         TEXT NOT NULL REFERENCES forecasts(forecast_hash),
    conditional_prob   REAL NOT NULL CHECK (conditional_prob > 0 AND conditional_prob < 1),
    -- Which dependence this is. Score correlation is not loss correlation.
    dependence_kind    TEXT NOT NULL CHECK (dependence_kind IN
                         ('score','loss','evidence')),
    correlation        REAL NOT NULL CHECK (correlation >= -1.0 AND correlation <= 1.0),
    created_at         TEXT NOT NULL,
    PRIMARY KEY (parent_hash, child_hash, dependence_kind),
    CHECK (parent_hash <> child_hash)
) STRICT;

CREATE TRIGGER edges_no_cycle BEFORE INSERT ON forecast_edges
BEGIN
    SELECT RAISE(ABORT, 'edge would create a cycle in the forecast DAG')
    WHERE EXISTS (
        WITH RECURSIVE reach(n) AS (
            SELECT NEW.child_hash
            UNION
            SELECT e.child_hash FROM forecast_edges e
            JOIN reach r ON e.parent_hash = r.n
            WHERE e.dependence_kind = NEW.dependence_kind
        )
        SELECT 1 FROM reach WHERE n = NEW.parent_hash
    );
END;

-- ============================================================================
-- 7. DECISION LAYER — exposure is capped, reality is not
-- ============================================================================

CREATE TABLE trade_decisions (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id            INTEGER NOT NULL REFERENCES forecasts(id),
    contract_id            INTEGER NOT NULL REFERENCES contracts(id),
    decided_at             TEXT NOT NULL,
    -- Executable, not midpoint.
    expected_acquisition_bp INTEGER CHECK (expected_acquisition_bp IS NULL OR
                              (expected_acquisition_bp > 0 AND expected_acquisition_bp < 10000)),
    intended_size_usd      REAL CHECK (intended_size_usd IS NULL OR intended_size_usd >= 0),
    costs_bp               INTEGER CHECK (costs_bp IS NULL OR costs_bp >= 0),
    -- EV computed against the CONSERVATIVE end of the interval, not the point.
    ev_per_share_bp        REAL,
    permitted              INTEGER NOT NULL CHECK (permitted IN (0,1)),
    abstain_reason         TEXT,
    -- Paper and live are different KINDS of record, not a flag on one kind. A
    -- paper decision is a counterfactual: it confers no permission and there is
    -- no order-management service for it to reach. Without this column the
    -- eligibility check below makes the entire UK close-only posture
    -- unrecordable -- every simulated decision would have to be stored as a
    -- refusal, destroying the counterfactual the paper run exists to produce.
    -- No DEFAULT: a decision that does not say whether it was real is not
    -- recordable. Defaulting to 'paper' would silently relabel an omitted
    -- live decision as a simulation, which is the direction that hides harm.
    mode                   TEXT NOT NULL CHECK (mode IN ('paper','live')),
    max_notional_usd       REAL NOT NULL CHECK (max_notional_usd >= 0),
    cluster_exposure_cap_usd REAL NOT NULL CHECK (cluster_exposure_cap_usd >= 0),
    eligibility_status     TEXT NOT NULL CHECK (eligibility_status IN
                             ('tradeable','close_only','blocked','unknown')),
    book_snapshot_ref      TEXT,
    CHECK (permitted = 1 OR abstain_reason IS NOT NULL),
    -- The invariant that matters, stated precisely: a LIVE permission requires
    -- a tradeable contract. Paper mode is exempt because it is not a permission.
    CHECK (permitted = 0 OR mode = 'paper' OR eligibility_status = 'tradeable')
) STRICT;

-- ============================================================================
-- 8. OUTCOMES — research outcome and economic settlement kept separate
-- ============================================================================

-- Resolutions are REVISED, never replaced. A disputed outcome that is later
-- adjudicated is a new revision with a reason and a named adjudicator; the
-- original row stays. The previous UNIQUE(proposition_id) forced a correction
-- to be a DELETE, which would have erased the fact that the outcome was ever in
-- doubt -- exactly what section 7.3 forbids for claims, applied one table over.
CREATE TABLE resolutions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposition_id     INTEGER NOT NULL REFERENCES propositions(id),
    revision           INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    outcome            TEXT NOT NULL CHECK (outcome IN
                         ('resolved_yes','resolved_no','void','disputed','unresolvable')),
    resolution_source  TEXT NOT NULL,
    resolved_at        TEXT,
    recorded_at            TEXT NOT NULL CHECK (recorded_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    -- Present from revision 2 onward: a correction must say why, and who.
    adjudication       TEXT,
    adjudicator        TEXT,
    UNIQUE (proposition_id, revision),
    CHECK (revision = 1 OR (adjudication IS NOT NULL AND adjudicator IS NOT NULL))
) STRICT;

CREATE TRIGGER resolutions_append_only BEFORE UPDATE ON resolutions
BEGIN SELECT RAISE(ABORT, 'resolutions are append-only; record a new revision'); END;

CREATE TRIGGER resolutions_no_delete BEFORE DELETE ON resolutions
BEGIN SELECT RAISE(ABORT, 'a resolution is never deleted; supersede it'); END;

-- The outcome as it currently stands. Everything that scores or evaluates reads
-- this, so a revision propagates without any query having to remember to.
CREATE VIEW current_resolutions AS
SELECT r.* FROM resolutions r
WHERE r.revision = (SELECT MAX(r2.revision) FROM resolutions r2
                    WHERE r2.proposition_id = r.proposition_id);

CREATE TABLE settlements (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id        INTEGER NOT NULL REFERENCES contracts(id),
    settlement_state   TEXT NOT NULL CHECK (settlement_state IN
                         ('settled','disputed','uma_escalated','pending','cancelled')),
    payout_per_share   REAL NOT NULL CHECK (payout_per_share >= 0.0 AND payout_per_share <= 1.0),
    settled_at         TEXT,
    recorded_at        TEXT NOT NULL,
    note               TEXT,
    UNIQUE (contract_id)
) STRICT;

-- A score records WHICH revision of the outcome it was computed against, so a
-- later adjudication produces a new score row rather than silently invalidating
-- an old one. Append-only, like everything else that constitutes the record: a
-- score that moved would undo the hash chain one table lower down.
CREATE TABLE scores (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_id        INTEGER NOT NULL REFERENCES forecasts(id),
    resolution_revision INTEGER NOT NULL DEFAULT 0 CHECK (resolution_revision >= 0),
    brier              REAL,             -- NULL when the outcome is not scorable
    scorable           INTEGER NOT NULL CHECK (scorable IN (0,1)),
    exclusion_reason   TEXT,
    computed_at            TEXT NOT NULL CHECK (computed_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    UNIQUE (forecast_id, resolution_revision),
    CHECK ((scorable = 1) = (brier IS NOT NULL)),
    CHECK (scorable = 1 OR exclusion_reason IS NOT NULL)
) STRICT;

CREATE TRIGGER scores_append_only BEFORE UPDATE ON scores
BEGIN SELECT RAISE(ABORT, 'scores are append-only; score against a new resolution revision'); END;

-- The score that currently stands for each forecast.
CREATE VIEW current_scores AS
SELECT s.* FROM scores s
WHERE s.resolution_revision = (SELECT MAX(s2.resolution_revision) FROM scores s2
                               WHERE s2.forecast_id = s.forecast_id);

-- ============================================================================
-- 9. SHADOW EXECUTION
-- v2 section 10.3: order-book collection and shadow fills run in parallel with
-- prospective forecasting from the start, because if execution eats the edge
-- you want to learn that in month one rather than month eighteen.
--
-- Nothing in this section can place an order. It is a measurement apparatus,
-- and under the section 2.1 paper-only posture it is the only execution there is.
-- ============================================================================

CREATE TABLE book_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id        INTEGER NOT NULL REFERENCES contracts(id),
    snapshot_hash      TEXT NOT NULL,     -- content address of the levels
    -- Same discipline as signal_items: the venue's own timestamp is a claim,
    -- captured_at is when we saw it, and only the third governs retrieval.
    venue_timestamp    TEXT,
    captured_at            TEXT NOT NULL CHECK (captured_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    available_for_decision_at TEXT NOT NULL CHECK (available_for_decision_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    bids               TEXT NOT NULL,     -- JSON [[price_bp,size_usd],...] best first
    asks               TEXT NOT NULL,
    source             TEXT NOT NULL CHECK (source IN
                         ('clob_rest','clob_ws','replay','fixture')),
    UNIQUE (contract_id, snapshot_hash, captured_at),
    CHECK (available_for_decision_at >= captured_at)
) STRICT;

CREATE INDEX idx_books_available
    ON book_snapshots (contract_id, available_for_decision_at);

CREATE TRIGGER book_snapshots_immutable BEFORE UPDATE ON book_snapshots
BEGIN SELECT RAISE(ABORT, 'book snapshots are immutable; capture a new one'); END;

CREATE TABLE shadow_orders (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id        INTEGER REFERENCES trade_decisions(id),
    contract_id        INTEGER NOT NULL REFERENCES contracts(id),
    book_snapshot_id   INTEGER NOT NULL REFERENCES book_snapshots(id),
    side               TEXT NOT NULL CHECK (side IN ('YES','NO')),
    -- Aggressive orders cross the spread and are priced by walking the book.
    -- Passive orders rest and are priced by queue position, which is a
    -- different question with a much less flattering answer.
    style              TEXT NOT NULL CHECK (style IN ('aggressive','passive')),
    limit_price_bp     INTEGER NOT NULL CHECK (limit_price_bp > 0 AND limit_price_bp < 10000),
    intended_size_usd  REAL NOT NULL CHECK (intended_size_usd > 0),
    placed_at          TEXT NOT NULL,
    -- Intent to cancel is not cancellation: an order remains fillable for the
    -- round trip. Fills inside that window are the ones that hurt.
    cancel_at          TEXT,
    cancel_latency_ms  REAL NOT NULL DEFAULT 0.0 CHECK (cancel_latency_ms >= 0.0),
    CHECK (style = 'aggressive' OR cancel_at IS NOT NULL)
) STRICT;

CREATE TABLE shadow_fills (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id           INTEGER NOT NULL UNIQUE REFERENCES shadow_orders(id),
    fill_model         TEXT NOT NULL CHECK (fill_model IN
                         ('book_walk','queue_position','no_fill')),
    filled_usd         REAL NOT NULL CHECK (filled_usd >= 0.0),
    avg_price_bp       REAL CHECK (avg_price_bp IS NULL OR
                                   (avg_price_bp > 0 AND avg_price_bp < 10000)),
    fully_filled       INTEGER NOT NULL CHECK (fully_filled IN (0,1)),
    -- Queue mechanics, recorded so a fill rate can be audited rather than trusted.
    queue_ahead_usd    REAL CHECK (queue_ahead_usd IS NULL OR queue_ahead_usd >= 0.0),
    traded_at_level_usd REAL CHECK (traded_at_level_usd IS NULL OR
                                    traded_at_level_usd >= 0.0),
    filled_after_cancel INTEGER NOT NULL DEFAULT 0
                         CHECK (filled_after_cancel IN (0,1)),
    no_fill_reason     TEXT,
    recorded_at        TEXT NOT NULL,
    CHECK (fill_model <> 'no_fill' OR (filled_usd = 0.0 AND no_fill_reason IS NOT NULL)),
    CHECK (filled_usd = 0.0 OR avg_price_bp IS NOT NULL)
) STRICT;

-- Measured against a LATER snapshot, so it cannot be computed at fill time.
-- Kept separate from shadow_fills for exactly that reason: a table whose rows
-- can only be written later should not look writable at the same moment.
CREATE TABLE fill_markouts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    fill_id            INTEGER NOT NULL REFERENCES shadow_fills(id),
    reference_snapshot_id INTEGER NOT NULL REFERENCES book_snapshots(id),
    horizon_seconds    REAL NOT NULL CHECK (horizon_seconds > 0),
    mid_at_fill_bp     REAL NOT NULL,
    mid_at_reference_bp REAL NOT NULL,
    -- Signed so POSITIVE means the market moved against the position taken.
    adverse_bp         REAL NOT NULL,
    computed_at        TEXT NOT NULL,
    UNIQUE (fill_id, horizon_seconds)
) STRICT;

-- ============================================================================
-- 10. COLLECTION
-- The anchor-first correction in docs/EVIDENCE-REPORT.md section 3: lexical
-- similarity finds duplication, never events, because independent reporting of
-- one event is lexically unrelated -- that is what makes it independent. So
-- items are retrieved FOR a registered proposition and the anchor is known at
-- ingest rather than inferred afterwards.
--
-- Which query produced an item is therefore provenance, not configuration, and
-- has to be in the record: without it the anchor is an assertion nobody can
-- check.
-- ============================================================================

CREATE TABLE collection_queries (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    proposition_id     INTEGER NOT NULL REFERENCES propositions(id),
    source_id          INTEGER NOT NULL REFERENCES sources(id),
    feed_url           TEXT NOT NULL,
    query_text         TEXT,             -- terms, where the feed takes them
    -- Predeclared, like a reference class: a query written after seeing which
    -- articles would have helped is a selection rule fitted to the outcome.
    declared_at            TEXT NOT NULL CHECK (declared_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    retired_at             TEXT CHECK (retired_at IS NULL OR retired_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    note               TEXT,
    UNIQUE (proposition_id, source_id, feed_url, query_text, declared_at)
) STRICT;

CREATE TABLE collection_runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id           INTEGER NOT NULL REFERENCES collection_queries(id),
    ran_at             TEXT NOT NULL,
    entries_seen       INTEGER NOT NULL CHECK (entries_seen >= 0),
    entries_ingested   INTEGER NOT NULL CHECK (entries_ingested >= 0),
    entries_duplicate  INTEGER NOT NULL CHECK (entries_duplicate >= 0),
    -- A run that failed is recorded as a run. A gap in the record that looks
    -- like "no news that day" is worse than a logged failure.
    outcome            TEXT NOT NULL CHECK (outcome IN ('ok','fetch_failed','parse_failed')),
    detail             TEXT,
    CHECK (entries_ingested <= entries_seen)
) STRICT;

-- Which query produced which item. Many-to-many: the same article legitimately
-- arrives through two queries, and knowing that is part of knowing how
-- independent the coverage is.
CREATE TABLE item_provenance (
    item_id            INTEGER NOT NULL REFERENCES signal_items(id),
    query_id           INTEGER NOT NULL REFERENCES collection_queries(id),
    run_id             INTEGER NOT NULL REFERENCES collection_runs(id),
    PRIMARY KEY (item_id, query_id, run_id)
) STRICT;

-- ============================================================================
-- 11. EVALUATION SCHEDULE
-- Section 12, and phase1/sequential_peeking.py, which measured what ignoring it
-- costs: checking a fixed 95% bound weekly for a year turns a ~3% gate into
-- 19.3%, a six-fold inflation, with no bad faith required.
--
-- The evaluation readout was committing exactly that error -- every run printed
-- a gate verdict from a plain 95% bootstrap interval, and running it on a
-- schedule is the peeking the simulation measured. So the gate now requires a
-- declared plan, and every look is recorded against it.
--
-- The number of looks must be DECLARED BEFORE the record accumulates. Choosing
-- it afterwards, once the shape of the data is visible, reintroduces precisely
-- the freedom the schedule removes -- so there is no way to evaluate a slice
-- that has no plan, and no way to add a look beyond the declared budget.
-- ============================================================================

CREATE TABLE evaluation_plans (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Which slice this plan governs: 'kind|model_version|event_family', or '*'
    -- for the whole record. One plan per slice; a second is a new plan for a
    -- new slice, never a revision of this one.
    slice_key          TEXT NOT NULL UNIQUE,
    n_looks            INTEGER NOT NULL CHECK (n_looks >= 1),
    alpha              REAL NOT NULL CHECK (alpha > 0.0 AND alpha < 1.0),
    spending           TEXT NOT NULL CHECK (spending IN ('pocock','obrien_fleming')),
    declared_at        TEXT NOT NULL CHECK (declared_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    declared_by        TEXT NOT NULL,
    note               TEXT
) STRICT;

CREATE TRIGGER evaluation_plans_immutable BEFORE UPDATE ON evaluation_plans
BEGIN SELECT RAISE(ABORT, 'an evaluation plan is immutable: changing the budget after looking is the error the plan exists to prevent'); END;

CREATE TRIGGER evaluation_plans_no_delete BEFORE DELETE ON evaluation_plans
BEGIN SELECT RAISE(ABORT, 'an evaluation plan is never deleted'); END;

CREATE TABLE evaluation_looks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id            INTEGER NOT NULL REFERENCES evaluation_plans(id),
    look_index         INTEGER NOT NULL CHECK (look_index >= 1),
    looked_at          TEXT NOT NULL CHECK (looked_at GLOB
                         '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9]Z'),
    n_observations     INTEGER NOT NULL CHECK (n_observations >= 0),
    n_regimes          INTEGER NOT NULL CHECK (n_regimes >= 0),
    point              REAL NOT NULL,
    std_error          REAL NOT NULL CHECK (std_error >= 0.0),
    z_threshold        REAL NOT NULL,
    fired              INTEGER NOT NULL CHECK (fired IN (0,1)),
    UNIQUE (plan_id, look_index)
) STRICT;

CREATE TRIGGER evaluation_looks_append_only BEFORE UPDATE ON evaluation_looks
BEGIN SELECT RAISE(ABORT, 'a look is a fact about a moment; it is not revised'); END;

CREATE TRIGGER evaluation_looks_no_delete BEFORE DELETE ON evaluation_looks
BEGIN SELECT RAISE(ABORT, 'deleting a look would un-spend alpha that was already spent'); END;

-- The budget is a hard limit, enforced here rather than in the caller.
CREATE TRIGGER evaluation_looks_within_budget BEFORE INSERT ON evaluation_looks
BEGIN
    SELECT RAISE(ABORT, 'look exceeds the declared number of looks for this plan')
    WHERE NEW.look_index > (SELECT n_looks FROM evaluation_plans
                            WHERE id = NEW.plan_id);
END;

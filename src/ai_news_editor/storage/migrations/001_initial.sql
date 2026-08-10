-- Phase 1 schema: provenance, triage, and the approval-bearing draft model.
--
-- Conventions:
--   * ids are TEXT (UUID4 as canonical string), source ids are human-readable slugs
--   * timestamps are TEXT, ISO-8601, always UTC and always offset-aware
--   * booleans are INTEGER 0/1
--   * enum-valued columns carry CHECK constraints so the database rejects statuses
--     the domain layer does not know about
--
-- Tables intentionally absent until the phase that uses them: evaluations (Phase 4),
-- publications and the publication queue (Phase 7+). See IMPLEMENTATION_PLAN.md.

CREATE TABLE sources (
    id                     TEXT PRIMARY KEY,
    name                   TEXT NOT NULL,
    kind                   TEXT NOT NULL CHECK (kind IN ('RSS', 'HTML_CHANGELOG', 'HN_SIGNAL')),
    url                    TEXT NOT NULL,
    trust_tier             TEXT NOT NULL CHECK (
                               trust_tier IN ('OFFICIAL', 'REPUTABLE_SECONDARY',
                                              'COMMUNITY_SIGNAL', 'UNVERIFIED')),
    signal_only            INTEGER NOT NULL DEFAULT 0 CHECK (signal_only IN (0, 1)),
    enabled                INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    language               TEXT NOT NULL DEFAULT 'en',
    publisher              TEXT,
    poll_interval_minutes  INTEGER NOT NULL DEFAULT 60 CHECK (poll_interval_minutes >= 1),
    config_json            TEXT NOT NULL DEFAULT '{}',
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    -- Community chatter signals attention, never fact. Mirrors the domain invariant.
    CHECK (trust_tier <> 'COMMUNITY_SIGNAL' OR signal_only = 1)
);

-- Immutable provenance anchor: exactly what a source returned.
CREATE TABLE raw_items (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    external_id     TEXT,
    title_original  TEXT,
    url_original    TEXT NOT NULL,
    author          TEXT,
    published_at    TEXT,
    fetched_at      TEXT NOT NULL,
    summary_raw     TEXT,
    content_raw     TEXT,
    payload_raw     TEXT NOT NULL,
    content_type    TEXT NOT NULL,
    fetch_run_id    TEXT
);

-- NULL external_id repeats freely in SQLite, which is what we want: only sources that
-- actually supply a stable id get deduplicated at the storage layer.
CREATE UNIQUE INDEX idx_raw_items_source_external
    ON raw_items (source_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX idx_raw_items_fetched_at ON raw_items (fetched_at);

CREATE TABLE articles (
    id               TEXT PRIMARY KEY,
    raw_item_id      TEXT NOT NULL UNIQUE REFERENCES raw_items (id) ON DELETE RESTRICT,
    source_id        TEXT NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    title            TEXT NOT NULL,
    canonical_url    TEXT NOT NULL,
    clean_text       TEXT,
    language         TEXT,
    published_at     TEXT,
    content_hash     TEXT,
    duplicate_of_id  TEXT REFERENCES articles (id) ON DELETE RESTRICT,
    status           TEXT NOT NULL CHECK (
                         status IN ('COLLECTED', 'NORMALIZED', 'DUPLICATE', 'SCREENED_OUT',
                                    'EVALUATED', 'SHORTLISTED', 'DRAFTED', 'DISCARDED')),
    filtered_by      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    CHECK (duplicate_of_id IS NULL OR duplicate_of_id <> id)
);

CREATE INDEX idx_articles_status ON articles (status);
CREATE INDEX idx_articles_canonical_url ON articles (canonical_url);
CREATE INDEX idx_articles_content_hash ON articles (content_hash);

-- Stable editorial identity. current_version_id is nullable only between creating the
-- draft and appending its first version.
CREATE TABLE drafts (
    id                  TEXT PRIMARY KEY,
    article_id          TEXT NOT NULL REFERENCES articles (id) ON DELETE RESTRICT,
    status              TEXT NOT NULL CHECK (
                            status IN ('DRAFTED', 'PENDING_REVIEW', 'NEEDS_REWRITE', 'APPROVED',
                                       'REJECTED', 'PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED')),
    current_version_id  TEXT REFERENCES draft_versions (id) ON DELETE RESTRICT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX idx_drafts_status ON drafts (status);
CREATE INDEX idx_drafts_article ON drafts (article_id);

-- Immutable content snapshots. Editing appends a version; it never rewrites one.
CREATE TABLE draft_versions (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL REFERENCES drafts (id) ON DELETE RESTRICT,
    version_no          INTEGER NOT NULL CHECK (version_no >= 1),
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    hashtags_json       TEXT NOT NULL DEFAULT '[]',
    -- Deliberately unconstrained: the category vocabulary is editorial and expected to
    -- evolve with the channel. It is validated by the domain enum. audience and status
    -- are structural, so those do get CHECK constraints.
    category            TEXT NOT NULL,
    audience            TEXT NOT NULL CHECK (
                            audience IN ('BEGINNER', 'GENERAL', 'TECH_CURIOUS')),
    source_attribution  TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    UNIQUE (draft_id, version_no)
);

CREATE INDEX idx_draft_versions_hash ON draft_versions (content_hash);

-- Append-only audit of human review actions, each bound to one exact version and the
-- content hash the human actually read.
CREATE TABLE review_decisions (
    id                TEXT PRIMARY KEY,
    draft_id          TEXT NOT NULL REFERENCES drafts (id) ON DELETE RESTRICT,
    draft_version_id  TEXT NOT NULL REFERENCES draft_versions (id) ON DELETE RESTRICT,
    content_hash      TEXT NOT NULL,
    action            TEXT NOT NULL CHECK (
                          action IN ('APPROVE', 'REJECT', 'EDIT', 'REQUEST_REWRITE', 'SKIP')),
    actor             TEXT NOT NULL,
    note              TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX idx_review_decisions_draft ON review_decisions (draft_id, created_at);
CREATE INDEX idx_review_decisions_version ON review_decisions (draft_version_id);

-- Append-only enforcement. These tables record what happened; rewriting history would
-- break provenance and would let approved content be swapped after the fact.
CREATE TRIGGER trg_raw_items_no_update BEFORE UPDATE ON raw_items
BEGIN SELECT RAISE(ABORT, 'raw_items is append-only'); END;

CREATE TRIGGER trg_raw_items_no_delete BEFORE DELETE ON raw_items
BEGIN SELECT RAISE(ABORT, 'raw_items is append-only'); END;

CREATE TRIGGER trg_draft_versions_no_update BEFORE UPDATE ON draft_versions
BEGIN SELECT RAISE(ABORT, 'draft_versions are immutable; append a new version instead'); END;

CREATE TRIGGER trg_draft_versions_no_delete BEFORE DELETE ON draft_versions
BEGIN SELECT RAISE(ABORT, 'draft_versions are immutable; append a new version instead'); END;

CREATE TRIGGER trg_review_decisions_no_update BEFORE UPDATE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END;

CREATE TRIGGER trg_review_decisions_no_delete BEFORE DELETE ON review_decisions
BEGIN SELECT RAISE(ABORT, 'review_decisions is append-only'); END;

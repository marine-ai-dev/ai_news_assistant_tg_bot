-- Phase 7.5: content model v2 — content types, editorial-original origin, NEWCOMER.
--
-- requires: table-rebuild
--
-- Migrations 001-006 are never edited. This one is unusual: three tables are rebuilt
-- rather than extended, because SQLite cannot widen a CHECK constraint or drop a NOT
-- NULL in place. The runner sets PRAGMA foreign_keys = OFF around this file and runs
-- PRAGMA foreign_key_check afterwards, refusing to leave a database whose references
-- stopped resolving. Every rebuild copies all columns verbatim; no stored value is
-- recomputed, and content hashes in particular are carried across untouched.
--
-- What changes and why:
--
--   1. drafts.article_id becomes nullable. A prompt or an explainer is written by the
--      editorial layer, not derived from someone else's article. Requiring an article
--      would mean inventing a source, which is the one thing provenance must never do.
--      A CHECK now enforces that a draft has exactly one origin, and that the origin
--      matches its content type.
--
--   2. draft_versions.audience and evaluations.audience accept NEWCOMER — a reader who
--      may have opened ChatGPT once. It sits below BEGINNER on the same scale.
--
--   3. content_items appears: the origin for editorial-original content, the way
--      articles are the origin for news.
--
-- What deliberately does not change: every existing row keeps its values, every
-- existing draft becomes content_type NEWS (which it is), and nothing reclassifies an
-- audience that a human or an evaluation already chose.

-- ---------------------------------------------------------------------------
-- 1. The origin for editorial-original content.
-- ---------------------------------------------------------------------------

CREATE TABLE content_items (
    id              TEXT PRIMARY KEY,
    content_type    TEXT NOT NULL CHECK (content_type IN ('PROMPT', 'EXPLAINER')),
    -- Named rather than implied. A reader of this table should not have to infer that
    -- a missing article means "we wrote it ourselves".
    origin          TEXT NOT NULL CHECK (origin IN ('EDITORIAL_ORIGINAL')),
    audience        TEXT NOT NULL CHECK (
                        audience IN ('NEWCOMER', 'BEGINNER', 'GENERAL', 'TECH_CURIOUS')),
    -- Working title. Not the published headline — that is written later and lives in
    -- draft_versions, where a human approves it.
    title           TEXT NOT NULL,
    -- PromptTopic for a prompt; NULL for an explainer, whose subject is its concept.
    topic           TEXT,
    -- The type-specific structure: prompt text and customization tips, or concept and
    -- explanation. Validated by a discriminated union in the domain layer. Columns
    -- would mean half of them NULL for every row, and nothing queries their contents.
    payload_json    TEXT NOT NULL,
    -- Optional factual references. Empty for an evergreen prompt that makes no claims
    -- about any product; populated for an explainer that describes what a tool does.
    -- Never dressed up as a source article.
    references_json TEXT NOT NULL DEFAULT '[]',
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,

    -- Re-importing the same editorial batch must not create a second copy.
    UNIQUE (content_type, title)
);

CREATE INDEX idx_content_items_type ON content_items (content_type, created_at);
CREATE INDEX idx_content_items_audience ON content_items (audience);

-- ---------------------------------------------------------------------------
-- 2. drafts: nullable article_id, content_type, and exactly one origin.
-- ---------------------------------------------------------------------------

CREATE TABLE drafts_new (
    id                  TEXT PRIMARY KEY,
    -- Nullable now. Set for NEWS, null for editorial-original content.
    article_id          TEXT REFERENCES articles (id) ON DELETE RESTRICT,
    content_item_id     TEXT REFERENCES content_items (id) ON DELETE RESTRICT,
    content_type        TEXT NOT NULL DEFAULT 'NEWS' CHECK (
                            content_type IN ('NEWS', 'PROMPT', 'EXPLAINER')),
    status              TEXT NOT NULL CHECK (
                            status IN ('DRAFTED', 'PENDING_REVIEW', 'NEEDS_REWRITE', 'APPROVED',
                                       'REJECTED', 'PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED')),
    current_version_id  TEXT REFERENCES draft_versions (id) ON DELETE RESTRICT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    evaluation_id       TEXT REFERENCES evaluations (id),

    -- One origin, matching the content type. This is what stops a prompt from
    -- borrowing an article's provenance, and stops a news draft from losing it.
    CHECK (
        (content_type = 'NEWS'
             AND article_id IS NOT NULL AND content_item_id IS NULL)
        OR (content_type IN ('PROMPT', 'EXPLAINER')
             AND content_item_id IS NOT NULL AND article_id IS NULL)
    ),
    -- An editorial-original draft has no article to evaluate, so it has no evaluation.
    CHECK (content_type = 'NEWS' OR evaluation_id IS NULL)
);

INSERT INTO drafts_new (id, article_id, content_item_id, content_type, status,
                        current_version_id, created_at, updated_at, evaluation_id)
SELECT id, article_id, NULL, 'NEWS', status, current_version_id, created_at, updated_at,
       evaluation_id
FROM drafts;

DROP TABLE drafts;

ALTER TABLE drafts_new RENAME TO drafts;

CREATE INDEX idx_drafts_status ON drafts (status);
CREATE INDEX idx_drafts_article ON drafts (article_id);
CREATE INDEX idx_drafts_evaluation ON drafts (evaluation_id);
CREATE INDEX idx_drafts_content_item ON drafts (content_item_id);
CREATE INDEX idx_drafts_content_type ON drafts (content_type, status);

-- ---------------------------------------------------------------------------
-- 3. draft_versions: audience gains NEWCOMER. Everything else is copied verbatim,
--    including content_hash — this rebuild must not change a single stored byte of
--    approved or published content.
-- ---------------------------------------------------------------------------

CREATE TABLE draft_versions_new (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL REFERENCES drafts (id) ON DELETE RESTRICT,
    version_no          INTEGER NOT NULL CHECK (version_no >= 1),
    title               TEXT NOT NULL,
    body                TEXT NOT NULL,
    hashtags_json       TEXT NOT NULL DEFAULT '[]',
    -- Still deliberately unconstrained; validated by the domain enum. See 001.
    category            TEXT NOT NULL,
    audience            TEXT NOT NULL CHECK (
                            audience IN ('NEWCOMER', 'BEGINNER', 'GENERAL', 'TECH_CURIOUS')),
    source_attribution  TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    post_format         TEXT CHECK (
                            post_format IS NULL
                            OR post_format IN ('QUICK', 'STANDARD', 'DEEP_DIVE')),
    source_url          TEXT,
    writer_notes_json   TEXT NOT NULL DEFAULT '[]',
    style_version       TEXT,

    UNIQUE (draft_id, version_no)
);

INSERT INTO draft_versions_new (id, draft_id, version_no, title, body, hashtags_json,
                                category, audience, source_attribution, content_hash,
                                created_by, created_at, post_format, source_url,
                                writer_notes_json, style_version)
SELECT id, draft_id, version_no, title, body, hashtags_json, category, audience,
       source_attribution, content_hash, created_by, created_at, post_format, source_url,
       writer_notes_json, style_version
FROM draft_versions;

DROP TABLE draft_versions;

ALTER TABLE draft_versions_new RENAME TO draft_versions;

CREATE INDEX idx_draft_versions_hash ON draft_versions (content_hash);

-- The append-only guarantee, restored. A rebuild that dropped these would quietly turn
-- the immutable content table into an editable one.
CREATE TRIGGER trg_draft_versions_no_update BEFORE UPDATE ON draft_versions
BEGIN
    SELECT RAISE(ABORT, 'draft_versions are immutable; append a new version instead');
END;

CREATE TRIGGER trg_draft_versions_no_delete BEFORE DELETE ON draft_versions
BEGIN
    SELECT RAISE(ABORT, 'draft_versions are immutable; append a new version instead');
END;

-- ---------------------------------------------------------------------------
-- 4. evaluations: audience gains NEWCOMER, so a news story can be judged as fitting a
--    reader with almost no AI literacy. Existing rows keep the audience they were
--    given; nothing is reclassified.
-- ---------------------------------------------------------------------------

CREATE TABLE evaluations_new (
    id                    TEXT PRIMARY KEY,
    article_id            TEXT NOT NULL REFERENCES articles (id) ON DELETE RESTRICT,
    schema_version        TEXT NOT NULL,
    rubric_version        TEXT NOT NULL,
    evaluator_type        TEXT NOT NULL CHECK (
                              evaluator_type IN ('CLAUDE_CODE', 'HUMAN', 'AUTOMATED')),
    evaluator             TEXT,
    batch_id              TEXT,
    content_fingerprint   TEXT NOT NULL,
    decision              TEXT NOT NULL CHECK (
                              decision IN ('SHORTLIST', 'HOLD_FOR_VERIFICATION', 'REJECT')),
    category              TEXT NOT NULL,
    audience              TEXT NOT NULL CHECK (
                              audience IN ('NEWCOMER', 'BEGINNER', 'GENERAL', 'TECH_CURIOUS')),
    credibility           INTEGER NOT NULL CHECK (credibility BETWEEN 0 AND 100),
    general_ai_relevance  INTEGER NOT NULL CHECK (general_ai_relevance BETWEEN 0 AND 100),
    reader_interest       INTEGER NOT NULL CHECK (reader_interest BETWEEN 0 AND 100),
    usefulness            INTEGER NOT NULL CHECK (usefulness BETWEEN 0 AND 100),
    novelty               INTEGER NOT NULL CHECK (novelty BETWEEN 0 AND 100),
    wow_factor            INTEGER NOT NULL CHECK (wow_factor BETWEEN 0 AND 100),
    virality_potential    INTEGER NOT NULL CHECK (virality_potential BETWEEN 0 AND 100),
    accessibility         INTEGER NOT NULL CHECK (accessibility BETWEEN 0 AND 100),
    consumer_impact       INTEGER NOT NULL CHECK (consumer_impact BETWEEN 0 AND 100),
    composite_score       REAL NOT NULL,
    verification_status   TEXT NOT NULL CHECK (
                              verification_status IN (
                                  'NOT_REQUIRED', 'VERIFIED', 'NEEDS_MORE_EVIDENCE')),
    verification_sources_json TEXT NOT NULL DEFAULT '[]',
    why_selected_json     TEXT NOT NULL DEFAULT '[]',
    editorial_angle       TEXT,
    notes                 TEXT,
    created_at            TEXT NOT NULL,

    UNIQUE (article_id, batch_id, content_fingerprint)
);

INSERT INTO evaluations_new SELECT
    id, article_id, schema_version, rubric_version, evaluator_type, evaluator, batch_id,
    content_fingerprint, decision, category, audience, credibility, general_ai_relevance,
    reader_interest, usefulness, novelty, wow_factor, virality_potential, accessibility,
    consumer_impact, composite_score, verification_status, verification_sources_json,
    why_selected_json, editorial_angle, notes, created_at
FROM evaluations;

DROP TABLE evaluations;

ALTER TABLE evaluations_new RENAME TO evaluations;

CREATE INDEX idx_evaluations_article ON evaluations (article_id, created_at);
CREATE INDEX idx_evaluations_decision ON evaluations (decision, composite_score);
CREATE INDEX idx_evaluations_batch ON evaluations (batch_id);

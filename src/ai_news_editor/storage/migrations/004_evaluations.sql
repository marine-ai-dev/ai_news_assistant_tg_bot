-- Phase 4: editorial evaluations.
--
-- Migrations 001-003 are never edited; everything here is additive.
--
-- Evaluations are append-only history. An article may be re-evaluated under a new
-- rubric, or after its content changes, and every earlier judgement stays queryable:
-- when was it evaluated, under which rubric, what was decided, and on what content.

CREATE TABLE evaluations (
    id                    TEXT PRIMARY KEY,
    article_id            TEXT NOT NULL REFERENCES articles (id) ON DELETE RESTRICT,

    -- Versioning. Stored per row so a rubric change can never silently reinterpret
    -- scores that were produced under different rules.
    schema_version        TEXT NOT NULL,
    rubric_version        TEXT NOT NULL,
    evaluator_type        TEXT NOT NULL CHECK (
                              evaluator_type IN ('CLAUDE_CODE', 'HUMAN', 'AUTOMATED')),
    evaluator             TEXT,
    batch_id              TEXT,

    -- Binds the judgement to the exact content that was reviewed. If the article is
    -- renormalized into different text this no longer matches, and the evaluation is
    -- reported as stale rather than standing in for a judgement nobody made.
    content_fingerprint   TEXT NOT NULL,

    decision              TEXT NOT NULL CHECK (
                              decision IN ('SHORTLIST', 'HOLD_FOR_VERIFICATION', 'REJECT')),
    category              TEXT NOT NULL,
    audience              TEXT NOT NULL CHECK (
                              audience IN ('BEGINNER', 'GENERAL', 'TECH_CURIOUS')),

    -- Explicit columns rather than a JSON blob: these are exactly the values ranking,
    -- gating and reporting query on.
    credibility           INTEGER NOT NULL CHECK (credibility BETWEEN 0 AND 100),
    general_ai_relevance  INTEGER NOT NULL CHECK (general_ai_relevance BETWEEN 0 AND 100),
    reader_interest       INTEGER NOT NULL CHECK (reader_interest BETWEEN 0 AND 100),
    usefulness            INTEGER NOT NULL CHECK (usefulness BETWEEN 0 AND 100),
    novelty               INTEGER NOT NULL CHECK (novelty BETWEEN 0 AND 100),
    wow_factor            INTEGER NOT NULL CHECK (wow_factor BETWEEN 0 AND 100),
    virality_potential    INTEGER NOT NULL CHECK (virality_potential BETWEEN 0 AND 100),
    accessibility         INTEGER NOT NULL CHECK (accessibility BETWEEN 0 AND 100),
    consumer_impact       INTEGER NOT NULL CHECK (consumer_impact BETWEEN 0 AND 100),

    -- Computed by Python from the components above, never supplied by the evaluator.
    composite_score       REAL NOT NULL,

    verification_status   TEXT NOT NULL CHECK (
                              verification_status IN (
                                  'NOT_REQUIRED', 'VERIFIED', 'NEEDS_MORE_EVIDENCE')),
    -- Small, bounded lists of short strings: JSON is the right shape here, and nothing
    -- ranks or filters on their contents.
    verification_sources_json TEXT NOT NULL DEFAULT '[]',
    why_selected_json     TEXT NOT NULL DEFAULT '[]',
    editorial_angle       TEXT,
    notes                 TEXT,

    created_at            TEXT NOT NULL,

    -- Importing the same reviewed file twice must not create a second row. A revised
    -- judgement gets a new batch id, so it is a new evaluation rather than a silent
    -- overwrite of the old one.
    UNIQUE (article_id, batch_id, content_fingerprint)
);

CREATE INDEX idx_evaluations_article ON evaluations (article_id, created_at);
CREATE INDEX idx_evaluations_decision ON evaluations (decision, composite_score);
CREATE INDEX idx_evaluations_batch ON evaluations (batch_id);

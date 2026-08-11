-- Phase 8.2 (continued): let drafts hold the two new formats.
--
-- requires: table-rebuild
--
-- The companion to migration 010, and the second half of the same mistake. Adding a
-- member to a Python enum widens nothing in SQLite: `drafts` carries three CHECK
-- constraints that name content types by hand, and all three had to be restated.
--
-- Worth recording, because the lesson generalises: after this, any change to
-- ContentType means grepping the schema for constraints that spell the values out.
-- Three CHECKs in this table alone do, and two of them are the origin rules — the ones
-- that stop a prompt borrowing an article's provenance. Those are exactly the
-- constraints that must not be quietly dropped while widening something else, so they
-- are restated below in full rather than relaxed.
--
-- Same rebuild procedure as 007 and 010: create, copy, drop, rename, foreign keys off
-- around the transaction, PRAGMA foreign_key_check afterwards. Every row copied
-- verbatim; no content hash is touched.

CREATE TABLE drafts_new (
    id                  TEXT PRIMARY KEY,
    article_id          TEXT REFERENCES articles (id) ON DELETE RESTRICT,
    content_item_id     TEXT REFERENCES content_items (id) ON DELETE RESTRICT,
    content_type        TEXT NOT NULL DEFAULT 'NEWS' CHECK (
                            content_type IN ('NEWS', 'PROMPT', 'EXPLAINER',
                                             'TESTED_USE_CASE', 'RESOURCE')),
    status              TEXT NOT NULL CHECK (
                            status IN ('DRAFTED', 'PENDING_REVIEW', 'NEEDS_REWRITE', 'APPROVED',
                                       'REJECTED', 'PUBLISHING', 'PUBLISHED', 'PUBLISH_FAILED')),
    current_version_id  TEXT REFERENCES draft_versions (id) ON DELETE RESTRICT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    evaluation_id       TEXT REFERENCES evaluations (id),

    -- One origin, matching the content type. Unchanged in force, restated to include
    -- the new types: news comes from an article, everything else from a content item,
    -- and never both. This is what stops a prompt borrowing an article's provenance.
    CHECK (
        (content_type = 'NEWS'
             AND article_id IS NOT NULL AND content_item_id IS NULL)
        OR (content_type IN ('PROMPT', 'EXPLAINER', 'TESTED_USE_CASE', 'RESOURCE')
             AND content_item_id IS NOT NULL AND article_id IS NULL)
    ),
    -- Editorial-original content has no article to evaluate, so it has no evaluation.
    CHECK (content_type = 'NEWS' OR evaluation_id IS NULL)
);

INSERT INTO drafts_new (id, article_id, content_item_id, content_type, status,
                        current_version_id, created_at, updated_at, evaluation_id)
SELECT id, article_id, content_item_id, content_type, status, current_version_id,
       created_at, updated_at, evaluation_id
FROM drafts;

DROP TABLE drafts;

ALTER TABLE drafts_new RENAME TO drafts;

CREATE INDEX idx_drafts_status ON drafts (status);
CREATE INDEX idx_drafts_article ON drafts (article_id);
CREATE INDEX idx_drafts_evaluation ON drafts (evaluation_id);
CREATE INDEX idx_drafts_content_item ON drafts (content_item_id);
CREATE INDEX idx_drafts_content_type ON drafts (content_type, status);

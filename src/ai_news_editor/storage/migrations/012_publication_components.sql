-- Phase 8.3: a publication is several messages, so its record is several rows.
--
-- Migrations 001-011 are never edited. This one adds a table and nothing else.
--
-- Until now a publication was one message and one row. A rich bundle is an image, the
-- post, a comment in the discussion group and a file — four Telegram calls with no
-- transaction across them. The existing `publications` row stays as the record of the
-- attempt as a whole; this table records what happened to each part.
--
-- That distinction is what makes resuming safe. "The post went out and the comment
-- failed" is not a failed publication and must never become one: re-sending the post
-- would put a second copy on the channel. A retry reads these rows, sees MAIN
-- SUCCEEDED, and sends only what is missing.
--
-- Append-only, like every other audit table here. A component record that can be
-- rewritten is a record that cannot answer "did that image actually go out?".

CREATE TABLE publication_components (
    id              TEXT PRIMARY KEY,
    publication_id  TEXT NOT NULL REFERENCES publications (id),
    draft_id        TEXT NOT NULL REFERENCES drafts (id),
    draft_version_id TEXT NOT NULL REFERENCES draft_versions (id),
    -- Which part of the bundle this row is about.
    component       TEXT NOT NULL CHECK (
                        component IN ('MAIN', 'MEDIA', 'COMMENT', 'RESOURCE')),
    -- The Bot API method used, recorded so the history says what was actually done
    -- rather than what the current code would do today.
    method          TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (
                        status IN ('SUCCEEDED', 'FAILED', 'UNCERTAIN', 'DEFERRED')),
    -- Telegram's own evidence. A comment lands in the discussion group, so its chat id
    -- differs from the channel's — both are recorded.
    message_id      INTEGER,
    chat_id         TEXT,
    failure_reason  TEXT,
    created_at      TEXT NOT NULL,

    -- A success must carry proof, except for DEFERRED, which is the honest record of a
    -- component that was never attempted (no discussion group linked, for instance).
    CHECK (status <> 'SUCCEEDED' OR message_id IS NOT NULL)
);

-- The question asked before every retry: which parts of this version already exist on
-- the channel? A partial index would be wrong here — FAILED and UNCERTAIN rows are
-- exactly what a resume needs to see.
CREATE INDEX idx_publication_components_version
    ON publication_components (draft_version_id, component, status);

CREATE INDEX idx_publication_components_publication
    ON publication_components (publication_id, created_at);

CREATE TRIGGER trg_publication_components_no_update
BEFORE UPDATE ON publication_components
BEGIN
    SELECT RAISE(ABORT, 'publication_components is append-only');
END;

CREATE TRIGGER trg_publication_components_no_delete
BEFORE DELETE ON publication_components
BEGIN
    SELECT RAISE(ABORT, 'publication_components is append-only');
END;

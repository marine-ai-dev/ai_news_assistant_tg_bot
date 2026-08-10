-- Phase 7: publication attempts.
--
-- Migrations 001-005 are never edited; everything here is additive.
--
-- This is the first table that records something the application did to the outside
-- world. Everything before it is reversible; a Telegram message is not. The table is
-- therefore an attempt log rather than a success log: failures and — most importantly —
-- attempts whose outcome was never learned are first-class rows. A publications table
-- holding only successes cannot answer "did that post actually go out?", which is the
-- only question that matters after a lost connection.
--
-- Authorization is deliberately NOT stored here. A PublishAuthorization is never
-- serialized: the persisted authority is the APPROVE row in review_decisions plus the
-- exact draft_versions row plus the draft's current status, and the gate reconstructs
-- an authorization from those on demand. A stored token would be a bearer credential
-- sitting in a file, which is exactly the thing the approval gate exists to avoid.

CREATE TABLE publications (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL REFERENCES drafts (id),
    draft_version_id    TEXT NOT NULL REFERENCES draft_versions (id),
    -- Which human approval this send rests on. Publication is the consequence of a
    -- decision, never a decision of its own.
    review_decision_id  TEXT NOT NULL REFERENCES review_decisions (id),
    -- What was actually sent, recorded independently of what the version row says now.
    content_hash        TEXT NOT NULL,
    -- The destination exactly as configured: '@channel' or a numeric chat id. Kept
    -- verbatim so the uniqueness rule below cannot be defeated by re-spelling it.
    channel             TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED', 'UNCERTAIN')),
    message_id          INTEGER,
    chat_id             TEXT,
    attempt_no          INTEGER NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    failure_reason      TEXT,
    published_at        TEXT,
    created_at          TEXT NOT NULL,

    -- A success must carry Telegram's own evidence that it happened. Enforced here as
    -- well as in the domain model, because this row is what a later run trusts when it
    -- decides whether to send again.
    CHECK (status <> 'SUCCEEDED' OR (message_id IS NOT NULL AND published_at IS NOT NULL))
);

-- The idempotency rule, and the reason this table exists.
--
-- One exact draft version may succeed at most once per destination. A partial index so
-- it constrains only successes: failed and uncertain attempts are expected to repeat,
-- and must stay recordable.
--
-- Note what this does NOT claim to prevent: two *different* versions of the same draft
-- can each be published, because each carries its own human approval. Re-approving
-- identical text produces a different version id and is therefore a different post —
-- which is correct, since a human explicitly approved it a second time.
CREATE UNIQUE INDEX idx_publications_one_success_per_destination
    ON publications (draft_version_id, channel)
    WHERE status = 'SUCCEEDED';

CREATE INDEX idx_publications_draft ON publications (draft_id, created_at);
CREATE INDEX idx_publications_status ON publications (status, created_at);

-- Append-only, like raw_items, draft_versions and review_decisions before it. An
-- attempt record that can be edited is an audit trail that can be rewritten, and this
-- particular trail is the evidence for whether something reached an audience.
CREATE TRIGGER trg_publications_no_update BEFORE UPDATE ON publications
BEGIN
    SELECT RAISE(ABORT, 'publications is append-only');
END;

CREATE TRIGGER trg_publications_no_delete BEFORE DELETE ON publications
BEGIN
    SELECT RAISE(ABORT, 'publications is append-only');
END;

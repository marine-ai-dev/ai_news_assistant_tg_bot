-- Phase 9: scheduling intent, kept separate from editorial state.
--
-- Migrations 001-012 are never edited. This one adds two tables and nothing else.
--
-- A draft has an editorial life: written, reviewed, approved, published. Wanting it to
-- go out on Thursday at ten is not a step in that life — it is a separate decision the
-- owner makes about an already-approved thing, and can change or withdraw without
-- touching the approval. So it gets its own row rather than another draft status.
--
-- The important column is `draft_version_id`. A queue item points at the exact version
-- a human read, never at "whatever is current when the scheduler wakes up". If the
-- draft is edited, that version stops being current, and this row can no longer
-- publish anything — which is the entire point. `content_hash` is the same guarantee
-- from the other direction: the row remembers what the approved bundle hashed to, so a
-- mismatch at publication time is caught rather than published.
--
-- The lease columns exist because two scheduler processes on one Mac is not a strange
-- situation, it is a Tuesday: a forgotten terminal, a restart, a launchd job. Claiming
-- is a single conditional UPDATE, so exactly one worker can win.

CREATE TABLE publication_queue (
    id                  TEXT PRIMARY KEY,
    draft_id            TEXT NOT NULL REFERENCES drafts (id),
    -- The exact approved version. Not the draft's current version at publication time.
    draft_version_id    TEXT NOT NULL REFERENCES draft_versions (id),
    -- The approval this scheduling rests on. If it stops being the latest approval for
    -- this version, the item is invalidated rather than quietly re-approved.
    review_decision_id  TEXT NOT NULL REFERENCES review_decisions (id),
    -- What the whole approved bundle hashed to, text and comment and media together.
    content_hash        TEXT NOT NULL,
    channel             TEXT NOT NULL,

    -- Canonical UTC. The machine's timezone is never consulted.
    scheduled_for       TEXT NOT NULL,
    -- What the owner meant, so the queue can be shown back in the timezone it was
    -- entered in rather than in whatever the default happens to be later.
    display_timezone    TEXT NOT NULL DEFAULT 'Europe/Kyiv',

    status              TEXT NOT NULL CHECK (
                            status IN ('SCHEDULED', 'PROCESSING', 'PUBLISHED',
                                       'CANCELLED', 'INVALIDATED',
                                       'STALE_REVIEW_REQUIRED', 'HOLD_FOR_REVIEW',
                                       'FAILED', 'UNCERTAIN')),
    -- Why an item stopped being publishable, in words meant for the owner. Always set
    -- when the status is one a human has to resolve.
    hold_reason         TEXT,

    -- Worker coordination. NULL when nobody holds the item.
    claimed_by          TEXT,
    claimed_at          TEXT,
    -- A claim that outlives its worker must expire, or a crashed process locks a post
    -- out of the channel forever. Recovery is safe because the component history, not
    -- this row, decides what was already sent.
    lease_expires_at    TEXT,

    -- The publication attempt this item produced, once it produced one.
    publication_id      TEXT REFERENCES publications (id),

    queued_at           TEXT NOT NULL,
    last_checked_at     TEXT,
    updated_at          TEXT NOT NULL,

    -- A human has to be able to resolve a held item, so the reason must be there.
    CHECK (status NOT IN ('INVALIDATED', 'STALE_REVIEW_REQUIRED', 'HOLD_FOR_REVIEW')
           OR hold_reason IS NOT NULL)
);

-- One live scheduling per approved version. Scheduling the same post twice for two
-- different times is not a feature, it is two copies on the channel. Cancelled,
-- invalidated and completed rows are excluded so a post can be rescheduled after being
-- withdrawn.
CREATE UNIQUE INDEX idx_publication_queue_one_active
    ON publication_queue (draft_version_id, channel)
    WHERE status IN ('SCHEDULED', 'PROCESSING');

-- The scheduler's only hot query: what is due, oldest first.
CREATE INDEX idx_publication_queue_due
    ON publication_queue (status, scheduled_for);

CREATE INDEX idx_publication_queue_draft
    ON publication_queue (draft_id, queued_at);

-- Why an item is where it is. Append-only, like every other audit table here: a
-- rescheduled post should still be able to say what time it was first meant for, and a
-- held one should say what held it, months later.
CREATE TABLE publication_queue_events (
    id              TEXT PRIMARY KEY,
    queue_id        TEXT NOT NULL REFERENCES publication_queue (id),
    -- QUEUED, RESCHEDULED, CANCELLED, CLAIMED, RELEASED, HELD, INVALIDATED,
    -- PUBLISHED, FAILED. Free text rather than a CHECK: this is a log, and a later
    -- phase adding a verb should not need a table rebuild.
    event           TEXT NOT NULL,
    -- Where the item went, when the event moved it.
    from_status     TEXT,
    to_status       TEXT,
    scheduled_for   TEXT,
    detail          TEXT,
    actor           TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_publication_queue_events_item
    ON publication_queue_events (queue_id, created_at);

CREATE TRIGGER trg_publication_queue_events_no_update
BEFORE UPDATE ON publication_queue_events
BEGIN
    SELECT RAISE(ABORT, 'publication_queue_events is append-only');
END;

CREATE TRIGGER trg_publication_queue_events_no_delete
BEFORE DELETE ON publication_queue_events
BEGIN
    SELECT RAISE(ABORT, 'publication_queue_events is append-only');
END;

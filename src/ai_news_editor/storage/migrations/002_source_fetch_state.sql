-- Phase 2: conditional-fetch bookkeeping and editorial provenance for sources.
--
-- Migration 001 is never edited; everything here is additive.

-- Per-source HTTP caching validators and the outcome of the last attempt.
-- One row per source, created lazily on the first fetch.
CREATE TABLE source_fetch_state (
    source_id             TEXT PRIMARY KEY REFERENCES sources (id) ON DELETE CASCADE,
    -- Validators echoed back on the next request. NULL when a server supplies
    -- neither, which is common: several feeds we use send no ETag at all, so
    -- correctness cannot depend on conditional GET working.
    etag                  TEXT,
    last_modified         TEXT,
    last_attempt_at       TEXT,
    last_success_at       TEXT,
    -- OK | NOT_MODIFIED | ERROR, mirroring the adapter's FetchOutcome.
    last_outcome          TEXT CHECK (
                              last_outcome IS NULL
                              OR last_outcome IN ('OK', 'NOT_MODIFIED', 'ERROR')),
    last_http_status      INTEGER,
    last_error            TEXT,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    updated_at            TEXT NOT NULL
);

-- Why a source is in the mix, and what kind of stories it is expected to supply.
-- Provenance and editorial metadata, not a truth signal: Phase 4 reads these
-- alongside trust_tier when deciding what to shortlist.
ALTER TABLE sources ADD COLUMN editorial_role TEXT;
ALTER TABLE sources ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]';

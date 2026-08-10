-- Phase 5: writing metadata on drafts.
--
-- Migrations 001-004 are never edited; everything here is additive.
--
-- The Draft / DraftVersion tables already exist from migration 001 and are not
-- reshaped. Four things Phase 5 genuinely persists have nowhere to live in that
-- schema:
--
--   * which editorial evaluation authorised the draft (provenance: a draft must be
--     traceable to the judgement that said the story was worth covering)
--   * the chosen post format, which drives length validation and the preview
--   * the machine-readable source URL, separate from the rendered attribution line
--   * internal writer notes, which are never published
--
-- Nothing here weakens the Phase-1 append-only triggers on draft_versions.

-- Provenance link from a draft back to the decision that authorised writing it.
ALTER TABLE drafts ADD COLUMN evaluation_id TEXT REFERENCES evaluations (id);

ALTER TABLE draft_versions ADD COLUMN post_format TEXT CHECK (
    post_format IS NULL OR post_format IN ('QUICK', 'STANDARD', 'DEEP_DIVE'));

-- The rendered attribution line lives in source_attribution and is part of the hashed
-- content. This column keeps the bare URL queryable and validatable on its own.
ALTER TABLE draft_versions ADD COLUMN source_url TEXT;

-- Internal notes for the reviewer: "availability unclear", "check rollout before
-- publishing". Deliberately outside the content hash — a note must not change what a
-- human is approving.
ALTER TABLE draft_versions ADD COLUMN writer_notes_json TEXT NOT NULL DEFAULT '[]';

-- Which style guide revision the text was written under, so a guide change never
-- silently reinterprets older prose.
ALTER TABLE draft_versions ADD COLUMN style_version TEXT;

CREATE INDEX idx_drafts_evaluation ON drafts (evaluation_id);

-- Phase 8.1: prompts must rest on a demonstration somebody actually published.
--
-- Migrations 001-007 are never edited. This one is purely additive: new columns with
-- defaults, and one index. No table is rebuilt, nothing is dropped, and every existing
-- row keeps every value it had.
--
-- The correction being made: Content Model v2 let a prompt be editorial-original, which
-- in practice meant inventing something plausible and publishing it as advice. A prompt
-- that reads well is not a prompt that was shown to work, and a reader cannot tell the
-- difference. From here, a prompt post carries the evidence that someone ran it.
--
-- Structured columns rather than one JSON blob, because a reviewer reads these fields
-- individually on a phone screen and because a missing one should be a validation
-- error rather than a paragraph that quietly omits it.

-- Publication eligibility, and a prompt concept only. Nullable, so an explainer carries
-- no evidence label rather than one that does not apply to it — an EXPLAINER marked
-- "LEGACY_UNVERIFIED" would read as a problem with the explainer, which it is not.
ALTER TABLE content_items ADD COLUMN evidence_status TEXT CHECK (
    evidence_status IS NULL
    OR evidence_status IN (
        'VERIFIED_SOURCE_BACKED', 'INSUFFICIENT_EVIDENCE', 'LEGACY_UNVERIFIED'));

-- The prompts written before this rule existed. Labelled for what they are: content
-- whose provenance cannot be reconstructed without inventing it. Not deleted, not
-- rewritten, and not retro-justified — just never publishable.
UPDATE content_items SET evidence_status = 'LEGACY_UNVERIFIED' WHERE content_type = 'PROMPT';

-- Where the demonstration lives, and how close it sits to whoever ran it.
ALTER TABLE content_items ADD COLUMN source_url TEXT;
ALTER TABLE content_items ADD COLUMN source_title TEXT;
ALTER TABLE content_items ADD COLUMN source_tier TEXT CHECK (
    source_tier IS NULL
    OR source_tier IN ('OFFICIAL_PRODUCT', 'REPUTABLE_WRITEUP', 'COMMUNITY_REPORT'));

-- Who tried it and with what. Stored as the source names them: a source that says
-- "ChatGPT" is recorded as "ChatGPT", never resolved into a model version we inferred.
ALTER TABLE content_items ADD COLUMN tested_by TEXT;
ALTER TABLE content_items ADD COLUMN tool_used TEXT;
ALTER TABLE content_items ADD COLUMN model_version TEXT;

-- What was asked, and what came back. The two fields a reviewer needs to judge whether
-- the demonstration is worth passing on.
ALTER TABLE content_items ADD COLUMN what_was_tested TEXT;
ALTER TABLE content_items ADD COLUMN observed_result TEXT;

-- What the source said did not work, and what the workflow depends on (file upload, a
-- paid plan, web search). Empty arrays are an honest "the source mentioned none".
ALTER TABLE content_items ADD COLUMN limitations_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE content_items ADD COLUMN requires_json TEXT NOT NULL DEFAULT '[]';

-- When the source was last looked at. Prompt behaviour changes under people.
ALTER TABLE content_items ADD COLUMN checked_at TEXT;

-- Whether the prompt in the post is the source's words, our rewording, or our
-- reconstruction of a workflow that was described rather than quoted. An adapted prompt
-- is never presented as a quotation.
ALTER TABLE content_items ADD COLUMN prompt_representation TEXT CHECK (
    prompt_representation IS NULL
    OR prompt_representation IN ('VERBATIM_SHORT', 'ADAPTED', 'WORKFLOW_RECONSTRUCTION'));

-- The eligibility question asked before every prompt publication.
CREATE INDEX idx_content_items_evidence ON content_items (content_type, evidence_status);

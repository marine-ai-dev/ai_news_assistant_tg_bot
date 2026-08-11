-- Phase 8.2: a post is more than its text.
--
-- Migrations 001-008 are never edited. This one is purely additive — new nullable
-- columns and one index — so every existing row keeps every value it had and no stored
-- content hash changes.
--
-- The channel this project automates existed before it, with a recognisable style: a
-- relevant emoji leading each block, prompts sometimes in the first comment, real result
-- images on practical posts, downloadable collections, and a forwarding call-to-action
-- at the end. That style is the product. Modelling only "headline plus body" would have
-- quietly turned it into a generic news feed.
--
-- What is stored here is the *publication bundle*: everything a human approves beyond
-- the post text. It is hashed with the text (see domain/content.py), because approving
-- a post and then changing its comment, its image or its footer would be approval of
-- something nobody read.
--
-- Backward compatibility is structural rather than promised. A version with no bundle
-- content hashes exactly as it did before this migration — the hash payload omits the
-- bundle key entirely when it is empty — so the approval behind the already-published
-- post still verifies.

-- ---------------------------------------------------------------------------
-- draft_versions: the approval-bound publication bundle.
-- ---------------------------------------------------------------------------

-- Where the prompt lives. NONE for the many posts that have no prompt at all.
ALTER TABLE draft_versions ADD COLUMN prompt_placement TEXT NOT NULL DEFAULT 'NONE'
    CHECK (prompt_placement IN ('INLINE', 'COMMENT', 'NONE'));

-- The first comment, published with the post. Approved together with it — never
-- generated afterwards, which is the whole reason it is a column and not a runtime step.
ALTER TABLE draft_versions ADD COLUMN comment_text TEXT;

-- Images, screenshots and files, as identity records: role, origin, reference. No
-- sizes, no modification times, no absolute paths — none of those change what a reader
-- receives, and hashing them would expire an approval because a file was touched.
ALTER TABLE draft_versions ADD COLUMN media_json TEXT NOT NULL DEFAULT '[]';

-- For a RESOURCE post: what the reader is being given.
ALTER TABLE draft_versions ADD COLUMN resource_json TEXT;

-- The channel call-to-action, frozen at creation rather than rendered from
-- configuration at send time. A config change must not be able to alter an approved
-- post, and the handle in particular must be exactly what the human saw.
ALTER TABLE draft_versions ADD COLUMN footer_text TEXT;

-- ---------------------------------------------------------------------------
-- content_items: use cases, resources, series.
-- ---------------------------------------------------------------------------

-- Which area of life a tested use case belongs to. NULL for everything else.
ALTER TABLE content_items ADD COLUMN use_case_theme TEXT;

-- What kind of act produced the evidence — a separate axis from source_tier, which says
-- who is vouching. A vendor demo and one person's Reddit post are both honest evidence
-- and are not the same claim; the writing has to say which it is.
ALTER TABLE content_items ADD COLUMN evidence_kind TEXT CHECK (
    evidence_kind IS NULL
    OR evidence_kind IN ('OFFICIAL_TEST', 'THIRD_PARTY_DEMO', 'COMMUNITY_TESTED',
                         'OWNER_TESTED', 'USER_REPORTED_LIFEHACK'));

-- Where a social source was found, and who posted it. Social posts disappear; this is
-- enough to say what was reviewed without copying somebody's whole post into our
-- database.
ALTER TABLE content_items ADD COLUMN source_platform TEXT;
ALTER TABLE content_items ADD COLUMN source_author TEXT;

-- Lightweight grouping: "7 днів AI-креативів", day 3. Metadata only — nothing schedules
-- or sequences on it, and this is not a campaign manager.
ALTER TABLE content_items ADD COLUMN series_name TEXT;
ALTER TABLE content_items ADD COLUMN series_order INTEGER CHECK (
    series_order IS NULL OR series_order >= 1);

CREATE INDEX idx_content_items_series ON content_items (series_name, series_order);

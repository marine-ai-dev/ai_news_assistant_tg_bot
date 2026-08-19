-- Step 3 (AI News Agent v2): editorial content-type and evidence classification.
--
-- Purely additive, same discipline as 008: two new nullable columns, no table rebuild,
-- no CHECK constraint. `evaluations.category` (migration 004) and
-- `draft_versions.category` (migration 007) are both already deliberately unconstrained
-- TEXT for exactly this reason — "the category vocabulary is editorial and expected to
-- evolve" (see 001's own comment on that column). `editorial_category` and
-- `evidence_type` follow the same precedent rather than repeating the `drafts.content_type`
-- mistake: that column's CHECK has already needed two full table-rebuild migrations
-- (007, 011) just to add a value, because the constraint spells out every member by
-- hand. This is the version of that lesson applied going forward, not just recorded.
--
-- Nullable: an evaluation made before this column existed carries no classification
-- rather than a fabricated one. Application code treats NULL editorial_category as NEWS
-- for every automated evaluation so far — true today (every automated Evaluation this
-- project has ever created for automation has been NEWS), not a guess.

ALTER TABLE evaluations ADD COLUMN editorial_category TEXT;
ALTER TABLE evaluations ADD COLUMN evidence_type TEXT;

CREATE INDEX idx_evaluations_editorial_category ON evaluations (editorial_category);

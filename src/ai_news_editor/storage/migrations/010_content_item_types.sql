-- Phase 8.2 (continued): let content_items hold the two new formats.
--
-- requires: table-rebuild
--
-- Migration 009 added the columns a tested use case and a resource need, and missed the
-- one thing that actually blocks them: migration 007's CHECK still says
-- content_type IN ('PROMPT', 'EXPLAINER'). Adding an enum member in Python does not
-- widen a constraint in SQLite, and the first real import found this immediately.
--
-- SQLite cannot alter a CHECK in place, so the table is rebuilt using the same procedure
-- as migration 007: create, copy, drop, rename, with foreign key enforcement off around
-- the transaction and PRAGMA foreign_key_check afterwards. Every column is copied
-- verbatim — including all the Phase-8.1 evidence, which is authoritative and is not
-- touched here.
--
-- The definition below is the whole table restated cleanly. Migrations 007-009 grew it
-- by ALTER, which leaves the DDL readable only in the sense that a transcript is
-- readable; this is a chance to write it out once.

CREATE TABLE content_items_new (
    id              TEXT PRIMARY KEY,
    -- The two formats added in Phase 8.2 join the two from Phase 7.5. NEWS is absent on
    -- purpose: news comes from an article, and a content item for it would be a
    -- fabricated source.
    content_type    TEXT NOT NULL CHECK (
                        content_type IN ('PROMPT', 'EXPLAINER', 'TESTED_USE_CASE', 'RESOURCE')),
    origin          TEXT NOT NULL CHECK (origin IN ('EDITORIAL_ORIGINAL')),
    audience        TEXT NOT NULL CHECK (
                        audience IN ('NEWCOMER', 'BEGINNER', 'GENERAL', 'TECH_CURIOUS')),
    title           TEXT NOT NULL,
    -- PromptTopic for a prompt. NULL for everything else: an explainer is described by
    -- its concept, a use case by its theme, a resource by its type.
    topic           TEXT,
    payload_json    TEXT NOT NULL,
    references_json TEXT NOT NULL DEFAULT '[]',
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,

    -- Phase 8.1: the demonstration a prompt or a use case rests on.
    evidence_status TEXT CHECK (
                        evidence_status IS NULL
                        OR evidence_status IN ('VERIFIED_SOURCE_BACKED',
                                               'INSUFFICIENT_EVIDENCE',
                                               'LEGACY_UNVERIFIED')),
    source_url      TEXT,
    source_title    TEXT,
    source_tier     TEXT CHECK (
                        source_tier IS NULL
                        OR source_tier IN ('OFFICIAL_PRODUCT', 'REPUTABLE_WRITEUP',
                                           'COMMUNITY_REPORT')),
    tested_by       TEXT,
    tool_used       TEXT,
    model_version   TEXT,
    what_was_tested TEXT,
    observed_result TEXT,
    limitations_json TEXT NOT NULL DEFAULT '[]',
    requires_json   TEXT NOT NULL DEFAULT '[]',
    checked_at      TEXT,
    prompt_representation TEXT CHECK (
                        prompt_representation IS NULL
                        OR prompt_representation IN ('VERBATIM_SHORT', 'ADAPTED',
                                                     'WORKFLOW_RECONSTRUCTION')),

    -- Phase 8.2: use-case theme, what kind of act produced the evidence, where a social
    -- source was found, and optional series grouping.
    use_case_theme  TEXT,
    evidence_kind   TEXT CHECK (
                        evidence_kind IS NULL
                        OR evidence_kind IN ('OFFICIAL_TEST', 'THIRD_PARTY_DEMO',
                                             'COMMUNITY_TESTED', 'OWNER_TESTED',
                                             'USER_REPORTED_LIFEHACK')),
    source_platform TEXT,
    source_author   TEXT,
    series_name     TEXT,
    series_order    INTEGER CHECK (series_order IS NULL OR series_order >= 1),

    UNIQUE (content_type, title)
);

INSERT INTO content_items_new
SELECT id, content_type, origin, audience, title, topic, payload_json, references_json,
       created_by, created_at, evidence_status, source_url, source_title, source_tier,
       tested_by, tool_used, model_version, what_was_tested, observed_result,
       limitations_json, requires_json, checked_at, prompt_representation,
       use_case_theme, evidence_kind, source_platform, source_author,
       series_name, series_order
FROM content_items;

DROP TABLE content_items;

ALTER TABLE content_items_new RENAME TO content_items;

CREATE INDEX idx_content_items_type ON content_items (content_type, created_at);
CREATE INDEX idx_content_items_audience ON content_items (audience);
CREATE INDEX idx_content_items_evidence ON content_items (content_type, evidence_status);
CREATE INDEX idx_content_items_series ON content_items (series_name, series_order);

-- Phase 3: deterministic normalization, duplicate detection, community signals.
--
-- Migrations 001 and 002 are never edited; everything here is additive.

-- Fingerprints and duplicate bookkeeping on the normalized article.
ALTER TABLE articles ADD COLUMN title_fingerprint TEXT;
ALTER TABLE articles ADD COLUMN simhash INTEGER;
ALTER TABLE articles ADD COLUMN duplicate_reason TEXT;
ALTER TABLE articles ADD COLUMN possible_duplicate_of_id TEXT REFERENCES articles (id);
ALTER TABLE articles ADD COLUMN normalized_at TEXT;

CREATE INDEX idx_articles_title_fingerprint ON articles (title_fingerprint);
CREATE INDEX idx_articles_duplicate_of ON articles (duplicate_of_id);

-- Near-duplicate candidate lookup is bounded by recency, not by hash banding.
--
-- Banding (splitting the simhash into fixed segments and matching on segment equality)
-- only guarantees it finds a pair when the Hamming distance is below the band count.
-- Measured separation on real headline pairs forces a threshold of 12, well above any
-- workable band count, so banding would silently miss genuine duplicates. Instead the
-- comparison set is every non-duplicate article in the recency window, which at MVP
-- volumes is a small indexed range scan.
CREATE INDEX idx_articles_status_created ON articles (status, created_at);

-- Community attention, kept deliberately apart from sources and articles.
--
-- A signal says "people are discussing this". It is never provenance for a claim and
-- never becomes article text. article_id is nullable: a discussion may be observed for
-- a story we hold no article for, which is worth keeping rather than discarding.
CREATE TABLE community_signals (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources (id) ON DELETE RESTRICT,
    external_id     TEXT NOT NULL,
    article_id      TEXT REFERENCES articles (id) ON DELETE RESTRICT,
    canonical_url   TEXT,
    title           TEXT,
    points          INTEGER,
    num_comments    INTEGER,
    author          TEXT,
    posted_at       TEXT,
    discussion_url  TEXT,
    observed_at     TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);

CREATE INDEX idx_community_signals_article ON community_signals (article_id);
CREATE INDEX idx_community_signals_url ON community_signals (canonical_url);

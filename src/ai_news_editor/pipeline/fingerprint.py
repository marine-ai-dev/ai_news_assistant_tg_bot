"""Deterministic content fingerprints for duplicate detection.

Three fingerprints, each answering a different question:

* ``content_fingerprint`` — are these byte-for-byte the same story after normalization?
* ``title_fingerprint`` — is this the same headline?
* ``simhash`` — are these *nearly* the same text?

SimHash is implemented here rather than pulled in as a dependency: it is about forty
lines, and having it in the repository makes it inspectable and testable, which matters
more than saving the code. No embeddings, no vector store, no model — the same input
always produces the same 64-bit integer, on any machine, in any Python process.
"""

from __future__ import annotations

import hashlib

from ai_news_editor.pipeline.text import tokenize

SIMHASH_BITS = 64

#: Features are single words *plus* adjacent word pairs. Measured on realistic headline
#: and summary pairs, this separates far better than either alone:
#:
#: =============================  ========  ========  =========
#: feature set                    near-dup  unrelated  separation
#: =============================  ========  ========  =========
#: unigrams only                     0–8      13–26     weak
#: unigrams + bigrams                0–10     22–32     strong
#: 3-word shingles                   0–18     29–34     unusable
#: =============================  ========  ========  =========
#:
#: Three-word shingles look appealing because they capture word order, but on short
#: news text a single changed word rewrites three features at once, so genuine
#: rewordings drift as far as unrelated stories. Bigrams keep enough word order to stop
#: "dog bites man" matching "man bites dog" without that sensitivity.
USE_BIGRAMS = True

#: Maximum Hamming distance still treated as a near-duplicate.
#:
#: Chosen from the measurements above: real near-duplicates reached 10, the closest
#: genuinely different pair was 22. Sitting at 12 catches the rewordings with margin
#: while staying far below anything that was actually a different story. Erring low is
#: deliberate — a false positive silently removes a real story from the candidate pool,
#: which is worse than letting a duplicate through to the editor.
DEFAULT_HAMMING_THRESHOLD = 12

#: Texts shorter than this produce unstable simhashes: with few shingles, a single word
#: change moves many bits, and unrelated short texts collide easily. They are excluded
#: from near-duplicate matching entirely rather than compared unreliably.
MIN_TOKENS_FOR_SIMHASH = 12


def content_fingerprint(title: str | None, text: str | None) -> str:
    """Stable fingerprint of a story's normalized title and body."""
    tokens = tokenize(f"{title or ''}\n{text or ''}")
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def title_fingerprint(title: str | None) -> str | None:
    """Stable fingerprint of a normalized title, or ``None`` when there is no title."""
    tokens = tokenize(title or "")
    if not tokens:
        return None
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def features(tokens: list[str]) -> list[str]:
    """Words plus adjacent word pairs — the feature set the simhash votes over."""
    if not tokens:
        return []
    if not USE_BIGRAMS:
        return list(tokens)
    bigrams = [f"{tokens[i]} {tokens[i + 1]}" for i in range(len(tokens) - 1)]
    return [*tokens, *bigrams]


def simhash(text: str) -> int | None:
    """Return the 64-bit simhash of ``text``, or ``None`` if it is too short to trust.

    Each feature is hashed to 64 bits and votes on every bit position — +1 for a set
    bit, -1 for a clear one. The sign of each column becomes the output bit, so texts
    sharing most features differ in only a few bits.
    """
    tokens = tokenize(text)
    if len(tokens) < MIN_TOKENS_FOR_SIMHASH:
        return None

    columns = [0] * SIMHASH_BITS
    for feature in features(tokens):
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for bit in range(SIMHASH_BITS):
            columns[bit] += 1 if value >> bit & 1 else -1

    result = 0
    for bit, column in enumerate(columns):
        if column > 0:
            result |= 1 << bit
    return result


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two simhashes."""
    return (left ^ right).bit_count()


def is_near_duplicate(
    left: int, right: int, threshold: int = DEFAULT_HAMMING_THRESHOLD
) -> bool:
    """Whether two simhashes are close enough to call the texts near-identical."""
    return hamming_distance(left, right) <= threshold


# Note on candidate lookup: an earlier design split the simhash into four 16-bit bands
# and searched by band equality. That trick relies on the pigeonhole principle, which
# only guarantees a shared band when the distance is below the band count — fine at a
# threshold of 3, silently lossy at 12. Since the measured threshold has to be 12, the
# bands were removed rather than kept as an index that quietly misses real duplicates.
#
# Near-duplicate candidates are instead bounded by the recency window (see
# NEAR_DUPLICATE_WINDOW in pipeline.dedupe) plus a hard row limit. At MVP volumes —
# hundreds of articles, of which only a fortnight is ever in scope — that is a small
# indexed range scan, not a table sweep. Exact-match layers stay fully indexed.

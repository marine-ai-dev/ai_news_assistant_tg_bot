"""Deterministic content fingerprinting for draft versions.

The approval gate binds a human's decision to a *content hash*, not to a draft id. If
the publishable content changes in any way, the hash changes, and the prior approval
stops applying. That property depends entirely on this hash being deterministic across
processes and runs, so the serialization here is explicit and sorted rather than
relying on ``hash()`` or dict ordering.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

#: Bumped only if the fields covered by the fingerprint change. Stored alongside the
#: hash so old approvals can never be silently reinterpreted under new rules.
CONTENT_HASH_VERSION = 1


def compute_content_hash(
    *,
    title: str,
    body: str,
    hashtags: Sequence[str],
    category: str,
    audience: str,
    source_attribution: str,
) -> str:
    """Return the SHA-256 fingerprint of everything a reviewer sees and approves.

    Every field that appears in the published post is covered. Hashtag order is
    preserved (it is visible output), but the payload is otherwise canonicalized so
    that identical content always produces an identical digest.
    """
    payload = {
        "v": CONTENT_HASH_VERSION,
        "title": title,
        "body": body,
        "hashtags": list(hashtags),
        "category": category,
        "audience": audience,
        "source_attribution": source_attribution,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


#: Bumped only if the fields covered by the editorial fingerprint change.
EDITORIAL_FINGERPRINT_VERSION = 1


def compute_editorial_fingerprint(
    *,
    title: str,
    canonical_url: str,
    excerpt: str | None,
    published_at: str | None,
) -> str:
    """Fingerprint of exactly what an editorial reviewer was shown.

    The same idea as :func:`compute_content_hash` for drafts, applied to evaluation: a
    judgement is bound to the content state it judged. If the article is later
    renormalized into different text the fingerprint changes, and the old evaluation is
    reported as stale rather than silently standing in for a judgement nobody made.
    """
    payload = "\n".join(
        [
            f"v={EDITORIAL_FINGERPRINT_VERSION}",
            f"title={title}",
            f"url={canonical_url}",
            f"published={published_at or ''}",
            f"excerpt={excerpt or ''}",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

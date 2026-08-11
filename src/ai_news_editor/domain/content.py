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
from collections.abc import Mapping, Sequence

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
    bundle: Mapping[str, object] | None = None,
) -> str:
    """Return the SHA-256 fingerprint of everything a reviewer sees and approves.

    Every field that appears in the published post is covered. Hashtag order is
    preserved (it is visible output), but the payload is otherwise canonicalized so
    that identical content always produces an identical digest.

    ``bundle`` carries the Phase-8.2 additions — a comment, media, a resource, the
    channel footer. A post is now more than its text, and an approval that covered only
    the text would let the comment or the image change after a human said yes.

    **The key is omitted entirely when the bundle is empty.** That is what keeps every
    hash computed before Phase 8.2 identical: a text-only post hashes exactly as it did,
    including the one already published to the channel. A version either has bundle
    content and hashes with it, or has none and hashes the way it always did — there is
    no third behaviour and no migration of stored digests.
    """
    payload: dict[str, object] = {
        "v": CONTENT_HASH_VERSION,
        "title": title,
        "body": body,
        "hashtags": list(hashtags),
        "category": category,
        "audience": audience,
        "source_attribution": source_attribution,
    }
    canonical = canonical_bundle(bundle)
    if canonical:
        payload["bundle"] = canonical
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_bundle(bundle: Mapping[str, object] | None) -> dict[str, object]:
    """Strip a bundle down to what publication actually depends on.

    Empty values are dropped rather than hashed as empties, so adding a field to the
    bundle vocabulary never changes the digest of content that does not use it.

    Deliberately excluded: anything about a file on this machine other than its
    identity. A media asset is hashed by role, origin and reference — not by size,
    modification time or absolute path, none of which change what a reader receives and
    all of which would make an approval expire for no reason.
    """
    if not bundle:
        return {}
    canonical: dict[str, object] = {}
    for key, value in bundle.items():
        if value in (None, "", [], (), {}):
            continue
        canonical[key] = list(value) if isinstance(value, tuple) else value
    return canonical


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

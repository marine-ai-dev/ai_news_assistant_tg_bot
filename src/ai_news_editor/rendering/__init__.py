"""The category-aware Telegram editorial renderer — Step 5 (AI News Agent v2).

Turns a semantic :class:`~rendering.content.EditorialContent` record (what Gemini or a
fixture produced — plain fields, no markup) into Telegram MarkdownV2 text
(:mod:`rendering.render`), following one centrally-defined visual identity
(:mod:`rendering.style`). Gemini never controls bold, links, emoji placement, or
parse_mode — see docs/style.md.
"""

"""A reading aid for content aimed at people who do not work in tech.

This is a **warning system, not a filter.** It flags terms a NEWCOMER is unlikely to
know and reports them to the reviewer; it never rejects a draft. That restraint is the
whole design. A keyword blocklist would fail on exactly the posts it should approve —
"Notion — сервіс для нотаток — додав…" contains the word Notion and is precisely the
right way to write it.

So the heuristic asks a narrower, checkable question: *does the term appear with an
explanation near it?* An unexplained term is a note for a human, who then decides.
Python cannot judge whether prose is understandable, and pretending otherwise would
either block good writing or wave through bad writing with a false sense of rigour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Terms a reader with almost no AI literacy is unlikely to know. Product names sit
#: alongside technical vocabulary because "Slack" is exactly as opaque as "API" to
#: someone who has never worked in an office that uses it.
JARGON: tuple[str, ...] = (
    "API",
    "Slack",
    "Notion",
    "GitHub",
    "Python",
    "токен",
    "контекстне вікно",
    "бенчмарк",
    "інференс",
    "агент",
    "AI-агент",
    "ШІ-агент",
    "промпт",
    "файнтюнінг",
    "ембединг",
    "ваги моделі",
    "опенсорс",
    "локальна модель",
    "плагін",
    "воркфлоу",
    "інтеграція",
)

#: Matching is stem-based, so a term is listed in its dictionary form only. Adding
#: "токени" alongside "токен" would just produce two notes for one word.
#:
#: A term counts as explained when one of these appears close to it. Ukrainian
#: explanatory constructions: a dash, "це", "тобто", "сервіс/застосунок/програма для".
_EXPLANATION_MARKERS = (
    " — ",
    " – ",
    " -- ",
    " це ",
    " це, ",
    "тобто",
    "сервіс",
    "застосунок",
    "програма",
    "інструмент",
    "означає",
    "простими словами",
    "(",
)

#: How far from the term an explanation may sit and still count. One clause, roughly.
_WINDOW = 90


@dataclass(frozen=True, slots=True)
class JargonNote:
    """One term that may need explaining, and whether it looks explained."""

    term: str
    explained: bool
    excerpt: str

    @property
    def message(self) -> str:
        if self.explained:
            return f"{self.term}: looks explained nearby"
        return f"{self.term}: used without an explanation nearby"


def scan(text: str, terms: tuple[str, ...] = JARGON) -> list[JargonNote]:
    """Find jargon in ``text`` and guess whether each occurrence is explained.

    Returns one note per distinct term, not per occurrence — a reviewer wants to know
    "is 'агент' explained anywhere?", not that it appears eleven times.

    A term counts as explained if *any* of its occurrences has an explanation nearby.
    Checking only the first would flag a post whose headline uses the word as a label
    and whose opening line then explains it, which is a perfectly good post.
    """
    notes: list[JargonNote] = []
    lowered = text.lower()

    for term in terms:
        # Ukrainian inflects: "агент" appears as "агента", "агентом", "агентів". Matching
        # the stem plus a short ending catches those without matching unrelated words.
        pattern = rf"(?<![\w-]){re.escape(term.lower())}[а-яіїєґ']{{0,4}}(?![\w-])"
        occurrences = list(re.finditer(pattern, lowered))
        if not occurrences:
            continue

        explained = False
        excerpt = ""
        for match in occurrences:
            start = max(0, match.start() - _WINDOW)
            end = min(len(text), match.end() + _WINDOW)
            window = text[start:end]
            if not excerpt:
                excerpt = " ".join(window.split())
            if any(marker in window.lower() for marker in _EXPLANATION_MARKERS):
                explained = True
                excerpt = " ".join(window.split())
                break

        notes.append(JargonNote(term=term, explained=explained, excerpt=excerpt))

    return notes


def unexplained(text: str, terms: tuple[str, ...] = JARGON) -> list[JargonNote]:
    """Only the terms that appear to be used without explanation."""
    return [note for note in scan(text, terms) if not note.explained]

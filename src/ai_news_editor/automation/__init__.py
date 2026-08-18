"""Unattended NEWS generation and publishing. Live publishing is gated by
``AI_NEWS_AUTOMATION_ENABLED``; a dry run or a send to the private test channel is not —
see :func:`~ai_news_editor.automation.pipeline.run_automation`'s docstring for why.

The collection and normalization *logic* this package drives (``pipeline.collect``,
``pipeline.process``) is unchanged and predates this package entirely. What is new here
is orchestrating them: :func:`~ai_news_editor.automation.pipeline.run_pass` is the one
place that decides which database a run actually writes to
(:func:`~ai_news_editor.automation.pipeline.isolated_connection`) before collection
even starts, so a dry run or a test send can run the *entire* real pipeline —
collection included — without ever touching the canonical database. Selection and
writing were, until this package existed, work only a Claude Code session did by
reading exported JSON and returning reviewed JSON by hand — there is still no code path
where that stops being true for ``PROMPT`` or ``TESTED_USE_CASE`` content, which this
package does not touch.

What changes here is narrower: for ``NEWS`` only, sourced only from configured
``OFFICIAL`` sources, this package plays the role a Claude Code session used to play —
producing the same :class:`~ai_news_editor.writing.schema.DraftResult` the human
pipeline already validates, approving it through the same
:func:`~ai_news_editor.publishing.gate.approve_draft` a human approval calls, under an
actor (``"gemini:auto"``) that is never mistaken for one. Publication goes out through
the same :func:`~ai_news_editor.publishing.service.publish_bundle` every other post in
this project uses. Nothing here sends to Telegram directly, and nothing here can.
"""

from __future__ import annotations

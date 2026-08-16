"""Editorial planning: what to publish next, and whether the week is balanced.

Deliberately a separate package from ``editorial/``. That layer evaluates candidates
*before* a draft exists and is forbidden by test from touching drafts or review
decisions at all. Planning is the opposite end of the pipeline: it reads approved
drafts, the publication queue and recorded approvals to describe a week.

Everything here is read-only. Nothing in this package writes a row.
"""

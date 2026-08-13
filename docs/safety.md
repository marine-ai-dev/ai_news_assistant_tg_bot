# 🔐 Safety by design

This project publishes to a real channel read by real people. Most of its engineering
effort goes into a single question:

> **Can anything reach the channel that a human did not read and approve, in exactly the
> form it goes out?**

The answer has to be no for reasons a test can demonstrate, not because the code is
written carefully. What follows is how each "no" is enforced.

---

## 1. 🛑 No auto-approval, no auto-publish

There is no code path that publishes without a human decision, and this is checked
mechanically rather than promised:

- `AI_NEWS_AUTO_PUBLISH_ENABLED` exists as a setting **so that its absence is testable**.
  Setting it to `true` raises at startup. There is nothing behind it.
- A test walks the **registered Typer options** of every command and fails if any of
  `--yes`, `-y`, `--all`, `--approve-all`, `--auto-approve`, `--force-approve` exists.
  It inspects the CLI object, not the source text, so a comment explaining that these
  flags do not exist cannot make the test pass.
- Every publication names **exactly one draft id**. There is no bulk form.

---

## 2. 🔗 Approval binds to an exact version, and to its hash

```
approve(draft_id, expected_version_id=…)
```

The version the human was looking at is passed in. If the draft moved on during a long
review session, the approval **fails** rather than silently approving different text.

The resulting `ReviewDecision` stores a `content_hash` covering the entire post:

```
title + body + hashtags + category + audience + source_attribution + bundle
```

where `bundle` is the comment, the media assets, the resource spec and the footer. Swap
an image or reword the first comment and the hash moves — so the approval no longer
verifies, and the gate refuses.

---

## 3. 🧊 Immutable version history

`draft_versions` is append-only, enforced by SQLite triggers, not by convention:

```sql
CREATE TRIGGER trg_draft_versions_no_update
BEFORE UPDATE ON draft_versions
BEGIN SELECT RAISE(ABORT, 'draft_versions is append-only'); END;
```

"What exactly did I approve last Tuesday?" is always answerable. A version that could be
edited in place would make the content hash meaningless.

---

## 4. ✏️ Editing invalidates approval

Appending a new version moves an `APPROVED` draft back to `PENDING_REVIEW`, in the same
transaction that writes the version. The new content is content nobody has approved, and
the system says so rather than inheriting the old decision.

---

## 5. 🎫 An authorization that cannot be forged

`PublishAuthorization` is a frozen dataclass whose `__post_init__` refuses to run unless
a `ContextVar` sentinel is set — and that sentinel is set only inside
`issue_publication_authorization()`, which the gate alone calls.

A publisher **receives** an authorization. It can never construct one. A test asserts
that direct construction raises.

---

## 6. ⏱ Re-verification immediately before sending

Between approving on a phone and confirming at a terminal, minutes or days can pass. So
the full check runs again as the last thing before any network call:

- the draft is still `APPROVED`
- the approved version is still the draft's current version
- the stored decision still refers to that version and that hash
- the evidence policy still passes
- no successful publication exists for this version and destination
- no earlier attempt is sitting in an unresolved state

Any failure stops the send. Nothing is "close enough".

---

## 7. 📑 Source-backed prompts

A prompt that reads well is not a prompt that was shown to work, and a reader cannot
tell the difference. So `PROMPT` and `TESTED_USE_CASE` posts must carry evidence:

- who tested it, with which tool, and what was actually observed
- a findable source URL for the demonstration
- any requirements or limitations worth stating

This is enforced at the **application level**, not as a prompt instruction: a draft
without acceptable evidence cannot be published, and **approval cannot override it**. A
human saying yes does not manufacture a demonstration that never happened.

---

## 8. 📵 The review bot is a front end, and owner-only

- **Authorization runs before anything touches a draft**, on every update. An
  unauthorized user gets four words and no data — not a redacted card, not a status
  count, nothing that confirms what exists.
- Every editorial action calls the same service layer the CLI calls. Nothing in the bot
  writes a draft status, builds a `ReviewDecision` or constructs an authorization.
- `callback_data` is treated as **evidence that a button was tapped, not evidence about
  state**. It carries an action, a draft prefix and the version number that was on
  screen; everything is re-read from the database. A tap on a card rendered before an
  edit does not approve the text that replaced it.
- Nothing sensitive is ever put in callback data: no post text, no URLs, no hashes, no
  owner id, no token.
- **The review bot cannot publish.** Approving and publishing are separate acts.

---

## 9. ♻️ Publish exactly once

A unique partial index makes idempotency a database fact rather than a hope:

```sql
CREATE UNIQUE INDEX idx_publications_one_success_per_destination
    ON publications (draft_version_id, channel)
    WHERE status = 'SUCCEEDED';
```

Failed and uncertain attempts remain recordable — they are expected to repeat. Only
success is constrained.

---

## 10. 🧩 Partial success is recorded, not smoothed over

A rich post is several Telegram calls with no transaction across them. Each component is
recorded separately as it completes. "The post went out and the comment failed" is a
**partial publication, not a failed one** — and re-sending the post to fix the comment
would be the one mistake here that readers can see and nobody can undo.

A retry reads the component history and sends only what is missing.

---

## 11. ❓ UNCERTAIN is a real state, and it stops everything

A send whose response was lost may or may not have produced a post. The only honest
thing an application can do is say so and stop:

- the attempt is recorded as `UNCERTAIN`
- the draft stays in `PUBLISHING`, which is not a publishable state
- **nothing is retried automatically**
- a human looks at the channel and resolves it

Preferring an incomplete post over a duplicated one is a deliberate trade, made
everywhere in this codebase.

---

## 12. 🧾 External content is untrusted data

Article text, titles, feed metadata and HTML come from other people's servers. They are
stored, quoted and attributed — never executed, never interpreted as instructions, and
never allowed to reach a shell. Editor invocation uses safe subprocess argument handling
rather than composed command strings.

---

## 13. 🤐 The token is never anywhere it could leak

The bot token is a `SecretStr` — printing the settings object cannot reveal it. Beyond
that, a logging filter scrubs token-shaped strings from **every** log record, including
non-string arguments such as `httpx.URL` objects, and the same redaction runs on any
failure reason before it is written to the database.

The token is absent from: logs, CLI output, exceptions, the database, test fixtures, git
history, HTTP error messages and generated documentation. A test asserts each of these.

---

## 🧪 How this is verified

The `tests/safety/` suite exists specifically for these properties, and is marked so it
can never be skipped by accident. Coverage of `publishing/gate.py` — the module that
decides whether anything may be published — is held at **100%**, as is the rich
publication path and the review bot.

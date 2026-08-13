# 🏗 Architecture

How the system is put together, and why the boundaries fall where they do.

---

## 🧭 The one idea everything else follows from

Three different kinds of actor touch a post on its way to a channel, and they are
**never** allowed to do each other's jobs:

| Actor | Does | Never does |
|---|---|---|
| 🐍 **Python** | Fetches, normalises, deduplicates, stores, validates, sends | Decides what is good, writes prose, approves |
| 🧠 **Claude Code** | Evaluates candidates, writes Ukrainian copy, researches evidence | Approves, publishes, writes to the database directly |
| 👤 **Human** | Approves, rejects, edits, publishes | — (nothing is done on their behalf) |

Claude Code is the **editorial operator**, not an API integration. There is no LLM
client library in this repository and no API key: the editorial and writing layers are a
Claude Code session exchanging JSON files with the application.

---

## 🔄 End-to-end flow

```mermaid
flowchart TD
    A["🌐 Sources<br/>RSS · Atom · changelogs"] --> B["📥 Collection<br/>conditional GET, per-source state"]
    B --> C["🧹 Normalize<br/>canonical URL, text extraction"]
    C --> D["🔁 Deduplicate<br/>URL + near-duplicate detection"]
    D --> E["🧠 Claude editorial batch<br/>JSON out → JSON in"]
    E --> F["✍️ Claude writing batch<br/>Ukrainian copy"]
    F --> G["🗃 Draft + immutable DraftVersion<br/>content hash computed here"]
    G --> H["📱 Human review<br/>CLI or private Telegram bot"]
    H -->|"❌ reject / 📝 rewrite / ✏️ edit"| G
    H -->|"✅ approve exact version"| I["🔐 PublishAuthorization"]
    I --> J["🛡 Publication gate<br/>re-verified immediately before sending"]
    J --> K["📣 Telegram channel"]

    style H fill:#1f6feb,color:#fff
    style I fill:#238636,color:#fff
    style J fill:#8957e5,color:#fff
```

The blue, green and purple boxes are the human-in-the-loop boundary. Nothing crosses
from `G` to `K` without a person having typed a decision about that exact version.

---

## 🧱 Layers

```mermaid
flowchart LR
    subgraph io["🔌 Edges"]
        S["sources/<br/>fetching"]
        T["publishing/telegram<br/>Bot API client"]
        B["bot/<br/>review bot"]
    end
    subgraph core["🧠 Core"]
        D["domain/<br/>models · enums · hashing"]
        P["pipeline/ processing/<br/>normalize · dedupe"]
        E["editorial/ writing/ content/<br/>batches in and out"]
        R["review/<br/>decisions"]
        G["publishing/<br/>gate · plan · rich · service"]
    end
    subgraph store["🗄 Storage"]
        Q["storage/<br/>repositories + ordered SQL migrations"]
    end

    S --> P --> Q
    E --> Q
    R --> Q
    B --> R
    G --> Q
    G --> T
    D -.-> core
```

`domain/` has no dependency on storage, HTTP or Telegram. Everything that decides
whether something may be published lives in `publishing/gate.py`, and every publisher
goes through it.

---

## 🔐 The publication safety flow

This is the part worth reading closely.

```mermaid
sequenceDiagram
    participant H as 👤 Human
    participant G as 🛡 Gate
    participant DB as 🗄 SQLite
    participant TG as 📣 Telegram

    H->>G: approve(draft, expected_version_id)
    G->>DB: is this exact version still current?
    G->>DB: does the evidence policy still pass?
    DB-->>G: yes
    G->>DB: record ReviewDecision (append-only)
    G-->>H: PublishAuthorization (content hash bound)

    Note over H,G: time passes — minutes or days

    H->>G: publish(draft)
    G->>DB: re-verify EVERYTHING against live state
    Note right of G: version still current?<br/>approval still valid?<br/>hash unchanged?<br/>evidence still passes?<br/>not already published?
    DB-->>G: all still true
    G->>TG: sendMessage / sendPhoto / sendDocument
    TG-->>G: message_id
    G->>DB: record Publication + per-component outcomes
```

Two properties fall out of this:

- **An approval is not a permit that travels forward in time.** It is a recorded fact
  about a specific version at a specific moment, re-checked in full at the last instant
  before anything leaves the machine.
- **A `PublishAuthorization` cannot be forged.** Its constructor refuses to run outside
  the gate — a `ContextVar` sentinel set only by `issue_publication_authorization()`. A
  publisher can receive one; it can never make one.

---

## 📦 The content model

```mermaid
flowchart TD
    A["Draft<br/><i>stable identity, mutable status</i>"] --> B["DraftVersion v1<br/><i>immutable</i>"]
    A --> C["DraftVersion v2<br/><i>immutable</i>"]
    A --> D["DraftVersion v3 ← current"]
    B -.->|"approved, then superseded"| X["❌ approval no longer applies"]
    D --> E["content_hash<br/><i>title + body + hashtags + category +<br/>audience + attribution + bundle</i>"]
    E --> F["PostBundle<br/>comment · media · resource · footer"]
```

A draft's **identity** is stable so it can be discussed and tracked. Its **content** is
immutable, so "what was approved" is always answerable. Editing appends a version and
returns the draft to `PENDING_REVIEW`; the old approval does not follow it.

The `content_hash` covers the **whole bundle**, not just the text. Change the comment,
swap an image, edit the footer — the hash moves and the approval stops verifying.

---

## 📨 Rich publication: several messages, no transaction

Telegram has no transaction across messages. A post with an image, a first comment and
a downloadable PDF is four API calls, and any of them can fail independently.

```mermaid
flowchart LR
    P["📋 Plan built<br/>before the first send"] --> M["1️⃣ MAIN<br/>sendMessage / sendPhoto"]
    M --> ME["2️⃣ MEDIA<br/>sendMediaGroup"]
    ME --> C["3️⃣ COMMENT<br/>→ discussion group"]
    C --> R["4️⃣ RESOURCE<br/>sendDocument"]
    M -.->|recorded| H[("publication_components<br/><i>append-only</i>")]
    ME -.->|recorded| H
    C -.->|recorded| H
    R -.->|recorded| H
    H --> RT["♻️ A retry reads this<br/>and sends only what is missing"]
```

- The **plan is decided before the first call**, so a dry run is the same function a
  real run uses — not a description of it.
- Every component is recorded as it completes. **The main post is never sent twice**: a
  resumed publication sees `MAIN → SUCCEEDED` and skips it.
- A component whose outcome is *unknown* blocks the whole resume. A duplicate post on a
  channel is visible to readers and cannot be undone; a missing comment is an annoyance.
  The system always prefers the annoyance.

---

## 📅 Scheduling: intent kept apart from editorial state

A draft has an editorial life — written, reviewed, approved, published. Wanting it to go
out on Thursday at ten is **not a step in that life**. It is a separate decision about an
already-approved thing, which the owner can change or withdraw without touching the
approval. So it gets its own row rather than another draft status.

```mermaid
flowchart TD
    A["Draft / DraftVersion"] --> B["✅ APPROVED"]
    B --> C["📅 PublicationQueueItem<br/><i>bound to the exact version + hash</i>"]
    C --> D{"⏰ due?"}
    D -->|"not yet"| C
    D -->|"yes"| E["🔒 atomic claim<br/><i>one worker wins</i>"]
    E --> F{"🛡 re-verify everything"}
    F -->|"all still true"| G["📨 rich publication service"]
    F -->|"edited"| H["INVALIDATED"]
    F -->|"aged out"| I["STALE_REVIEW_REQUIRED"]
    F -->|"missing file · unresolved attempt"| J["HOLD_FOR_REVIEW"]
    G --> K["📣 Telegram"]
    G -.->|"lost response"| L["UNCERTAIN — never retried"]

    style F fill:#8957e5,color:#fff
    style H fill:#d29922,color:#000
    style I fill:#d29922,color:#000
    style J fill:#d29922,color:#000
    style L fill:#da3633,color:#fff
```

Every amber and red state ends in a human's hands. The scheduler resolves none of them.

### The status model

Nine states, and no more, because a scheduler with dozens of states is one nobody can
reason about:

| Status | Meaning |
|---|---|
| `SCHEDULED` | waiting for its time |
| `PROCESSING` | a worker holds the lease and is publishing it now |
| `PUBLISHED` | it went out |
| `CANCELLED` | the owner withdrew it; the approval is untouched |
| `INVALIDATED` | what it pointed at changed; it can never publish |
| `STALE_REVIEW_REQUIRED` | the content aged past its window — an editorial judgement |
| `HOLD_FOR_REVIEW` | a precondition failed — operational rather than editorial |
| `FAILED` | Telegram definitely refused; nothing is on the channel |
| `UNCERTAIN` | the outcome is unknown; never retried by a machine |

`DUE` is **computed from the clock**, never stored — a stored `DUE` goes stale by
definition.

### Why the exact-version binding is the whole design

A queue row stores `draft_version_id`, not just `draft_id`. "Publish this draft on
Thursday" would mean publishing whatever the draft says on Thursday, including edits
nobody approved. "Publish *this version* on Thursday" can only ever publish what a human
actually read — and becomes unpublishable the moment that version stops being current.

Appending a new version invalidates any waiting schedule **in the same transaction that
writes the version**. That placement is the point: the guarantee must not depend on
remembering to call a service afterwards.

### Worker coordination

```mermaid
sequenceDiagram
    participant A as ⚙️ worker A
    participant B as ⚙️ worker B
    participant DB as 🗄 SQLite

    A->>DB: UPDATE … SET status='PROCESSING' WHERE status='SCHEDULED' AND due
    B->>DB: UPDATE … SET status='PROCESSING' WHERE status='SCHEDULED' AND due
    DB-->>A: 1 row
    DB-->>B: 0 rows
    Note over B: not an error — it simply moves on,<br/>and says nothing to Telegram
    A->>DB: publish, record every component
```

One conditional UPDATE, and the condition *is* the safety property. There is no window
between deciding and claiming, because they are the same statement.

---

## 🗄 Storage

Plain `sqlite3` from the standard library, with hand-written ordered SQL migrations and
checksums. No ORM.

- **WAL** mode, foreign keys enforced.
- **Append-only tables** guarded by triggers: `raw_items`, `draft_versions`,
  `review_decisions`, `publications`, `publication_components`. A record that can be
  rewritten cannot answer "did that post actually go out?".
- **A unique partial index** enforces publish-exactly-once: one exact `draft_version_id`
  may succeed at most once per destination. Failed and uncertain attempts stay
  recordable, because they are expected to repeat.

---

## 🚫 What is deliberately absent

| Not here | Why |
|---|---|
| An LLM API client | Claude Code is the operator, not a dependency |
| An ORM | The schema is the point; migrations are read as prose |
| A task queue / broker | One person, one Mac, one channel |
| An auto-publish flag | There is a setting, and it is rejected at startup |
| A "publish all" command | Every publication names exactly one draft |

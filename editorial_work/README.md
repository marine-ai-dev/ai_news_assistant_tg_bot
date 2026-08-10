# Editorial working directory

`ai-news editorial export` writes batch files here, and reviewed files are written
alongside them.

The JSON files are **git-ignored**. They are working artefacts containing article text
fetched from the internet, and everything they decide is persisted in SQLite by
`ai-news editorial import` — the files themselves are not the record.

See [CLAUDE_EDITORIAL_WORKFLOW.md](../CLAUDE_EDITORIAL_WORKFLOW.md) for the review loop
and [docs/editorial_rubric.md](../docs/editorial_rubric.md) for the scoring standard.

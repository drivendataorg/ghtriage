---
name: ghtriage
description: >-
  Query a local snapshot of a GitHub repository's issues, pull requests, and
  comments via the ghtriage CLI. Use whenever the user asks about
  issue triage or project management on a repository — stale or unanswered
  issues, likely duplicates ("has this been reported before?"), backlog
  grooming, pull requests waiting on review, release notes, contributor
  activity — even if they never mention ghtriage. Prefer it over the GitHub
  API or gh CLI whenever a question spans many issues or PRs; use gh for
  writes, for the live state of a single item (latest comments on an active
  PR), and for what ghtriage does not store (diffs, CI checks, review
  verdicts).
license: MIT
compatibility: Requires the ghtriage CLI (Python 3.10+). Network access and a GitHub token are needed only for `ghtriage pull`.
metadata:
  managed-by: ghtriage
---

# ghtriage

ghtriage keeps a local DuckDB snapshot of one GitHub repository's issues, pull
requests, and comments, and lets you query it with SQL in milliseconds instead
of paging the GitHub API. It provides facts, not judgments: it pre-computes
things like comment counts and response timestamps, but "stale" or "needs
attention" is for you to define from the project's context and express as SQL.

## ghtriage or gh?

Pick by breadth and freshness. ghtriage answers questions about the corpus;
`gh` answers questions about a specific live conversation.

- Questions spanning many issues or PRs — filtering, counting, searching,
  ranking — are ghtriage's home ground: one SQL query replaces dozens of
  paginated API calls, and a few hours of snapshot staleness rarely changes an
  aggregate answer.
- The live state of one item you already know is `gh` territory — the latest
  comments on a PR under active development, where the snapshot is stale by
  construction and one `gh pr view` is cheaper than a pull. So is anything
  ghtriage does not store at all: diffs, CI and check results, review
  approval verdicts.
- Broad *and* freshness-critical at once: run `ghtriage pull` first
  (incremental, seconds), then query.

## Workflow

Run commands from the project root (the directory holding `.ghtriage/`).

1. **`ghtriage status`** — always first. The database is a snapshot that never
   updates on its own; status shows which repository it holds and when it was
   last pulled. Querying without checking freshness silently answers from old
   data.
2. **`ghtriage pull`** — if the database is missing or stale. Pulls are
   incremental and cheap to re-run. Authentication comes from `GITHUB_TOKEN`,
   a `.ghtriage/token` file, or an opt-in `gh` CLI fallback; run
   `ghtriage auth status` to see every source and which one wins. If none is
   configured, ask the user to run `ghtriage auth setup` — it is an
   interactive command for a human, and tokens are credentials you should not
   be handling. When pull fails with an HTTP 401/403/404, it prints a
   diagnosis and next steps on stderr — relay that to the user instead of
   retrying. If pull refuses and asks for `--full` after a version upgrade,
   that is expected — run `ghtriage pull --full` once.
3. **`ghtriage schema`** — before writing SQL. Tables exist only if data
   exists (a repo with no PRs has no `pull_requests` table), so never assume;
   the schema listing is the authoritative inventory, including the full-text
   indexes. `ghtriage schema --table NAME` shows per-column documentation and,
   for child tables, the join key.
4. **`ghtriage query "SQL"`** — `--format json` emits JSONL (one object per
   row), the best format for you to parse; the default `table` format is best
   when showing results to the user. Use `LIMIT` while exploring. SQL string
   literals use single quotes, so wrap the statement in double quotes for the
   shell.

Exit codes: `0` success, `1` runtime failure (missing database, SQL error),
`2` usage error.

## Writing queries

- **Start from the derived tables.** `issue_activity` and
  `pull_request_activity` have one row per issue/PR with labels, assignees,
  comment counts, participant counts, and response timestamps already joined —
  most triage questions need no hand-written joins at all.
- **Child tables** hold nested arrays and are named with a double underscore:
  `issues__labels` joins on `issue_number = issues.number`. Run
  `schema --table` to see any child table's join key.
- **Bot activity is split out, not filtered.** `comment_count` counts
  everything; `non_bot_comment_count` counts only non-bot accounts. Pick the
  one that matches the question ("has anyone responded?" usually means
  non-bot).
- **PRs have two comment channels.** Conversation comments (the discussion
  thread) and review comments (inline on the diff) are separate columns in
  `pull_request_activity` — a PR can have lots of one and none of the other.

## Full-text search

Every pull builds BM25 indexes. Pick the index by the question, not the table:

- "Is there an issue *about* this?" → `fts_github_issues` /
  `fts_github_pull_requests` (title + body only).
- "Has this been *discussed* anywhere?" → `fts_github_issue_threads` /
  `fts_github_pull_request_threads` (title + body + every comment).
- "Which *comment* said it?" → `fts_github_conversation_comments` /
  `fts_github_review_comments` (one document per comment).

The canonical query shape — the score function is keyed on the document `id`,
which the derived tables also carry, so search and facts combine in one query:

```sql
SELECT number, title, state, score FROM (
    SELECT *, fts_github_issue_threads.match_bm25(id, 'search terms') AS score
    FROM issue_activity
) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10
```

Pitfalls that will silently give wrong results:

- **Digits are not indexed.** Searching `404` or `v2.1` finds nothing — use
  `LIKE` or `regexp_matches` on the text columns for error codes, versions,
  and identifiers.
- **Terms are OR-ed and stemmed.** `'azure credential'` matches documents with
  either word. Pass `conjunctive := 1` as a third argument to require all.
- **Scores only rank within one query.** They are not comparable across
  queries or indexes — take the top N, never filter on a threshold.

## Worked recipes

For complete queries answering common triage questions — duplicate checks,
unanswered issues, stale-issue reports, PRs awaiting review, release notes —
read [references/query-cookbook.md](references/query-cookbook.md).

## If ghtriage is not installed

Install it as a tool (`uv tool install git+https://github.com/drivendataorg/ghtriage`)
or run it without installing:
`uvx --from git+https://github.com/drivendataorg/ghtriage ghtriage --help`.

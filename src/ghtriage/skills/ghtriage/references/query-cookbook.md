# ghtriage query cookbook

Complete queries for common triage questions. Each recipe is a starting point:
the thresholds and label names are placeholders to replace with values that fit
the project — ghtriage deliberately does not define "stale" or "important" for
you. Run any of these as `ghtriage query "..."` (double quotes for the shell;
SQL string literals inside use single quotes). Add `--format json` when you are
parsing the output rather than showing it.

Column inventory used below comes from the derived tables; confirm with
`ghtriage schema --table issue_activity` and
`ghtriage schema --table pull_request_activity` — column availability is
authoritative there, and a repo with no PRs has no PR tables at all.

## Recipes

1. [Has this been reported before?](#1-has-this-been-reported-before)
2. [Which comment said it?](#2-which-comment-said-it)
3. [Searching for exact codes and versions](#3-searching-for-exact-codes-and-versions)
4. [Open issues nobody has responded to](#4-open-issues-nobody-has-responded-to)
5. [Stale open issues](#5-stale-open-issues)
6. [Triage by label, and the unlabeled](#6-triage-by-label-and-the-unlabeled)
7. [Pull requests waiting on review](#7-pull-requests-waiting-on-review)
8. [Most-discussed open issues](#8-most-discussed-open-issues)
9. [Release notes: merged since a date](#9-release-notes-merged-since-a-date)
10. [Search results filtered by facts](#10-search-results-filtered-by-facts)

---

### 1. Has this been reported before?

Search whole threads (title, body, and every comment), so a mention deep in a
long discussion still matches. Do not restrict to open issues — a closed match
may hold the answer or justify reopening.

```sql
SELECT number, title, state, score FROM (
    SELECT *, fts_github_issue_threads.match_bm25(id, 'crash uploading large files') AS score
    FROM issue_activity
) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10
```

Read the ranking, not the match count: with OR-ed terms, a common word matches
many documents. If the top results look diffuse, retry with
`conjunctive := 1` as a third argument to require every term. To match only
what authors themselves described (ignoring comment noise), swap in
`fts_github_issues`.

### 2. Which comment said it?

Score individual comments instead of whole threads — a single comment that
says it all outranks a long thread that circles the topic.

```sql
SELECT number, commenter, excerpt, score FROM (
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS number,
        user__login AS commenter,
        substr(body, 1, 200) AS excerpt,
        fts_github_conversation_comments.match_bm25(id, 'workaround environment variable') AS score
    FROM conversation_comments
) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10
```

`conversation_comments` covers discussion on both issues and PRs; use
`review_comments` (keyed by `pull_request_url`) for inline code-review
comments.

### 3. Searching for exact codes and versions

The full-text tokenizer strips digits, so `ERR_413` or `v2.1.0` finds nothing
through `match_bm25`. Match the text directly:

```sql
SELECT a.number, a.title, a.state
FROM issue_threads t JOIN issue_activity a USING (id)
WHERE t.thread_text LIKE '%ERR_UPLOAD_413%'
```

The thread tables (`issue_threads`, `pull_request_threads`) exist for exactly
this: one row per issue/PR with `thread_text` folding in the title, body, and
all comments. They carry only `id`, `number`, and `thread_text` — join back to
the activity view (they share `id`) for anything else. Use `ILIKE` for
case-insensitive matching or `regexp_matches` for patterns.

### 4. Open issues nobody has responded to

`first_non_author_comment_at` is NULL when no one but the author has
commented — including when the author has been talking to themselves.

```sql
SELECT number, title, author, created_at, comment_count
FROM issue_activity
WHERE state = 'open' AND first_non_author_comment_at IS NULL
ORDER BY created_at ASC
```

Oldest first, since the oldest unanswered item is usually the most overdue.
Note this counts bot replies as responses; if a bot's auto-reply should not
count as "answered", also check `non_bot_comment_count`.

### 5. Stale open issues

There is no built-in "stale" — pick the signal and threshold that fit the
project. `updated_at` is GitHub's own last-touched timestamp (any edit, label,
or comment); `last_comment_at` is conversation only.

```sql
SELECT number, title, updated_at, last_comment_at, non_bot_comment_count
FROM issue_activity
WHERE state = 'open' AND updated_at < now() - INTERVAL 90 DAY
ORDER BY updated_at ASC
```

Remember the database is a snapshot: `now()` is query time, not pull time, so
check `ghtriage status` first — a month-old snapshot makes everything look a
month staler than it is.

### 6. Triage by label, and the unlabeled

`labels` is a list column, already joined:

```sql
SELECT number, title, non_bot_comment_count, updated_at
FROM issue_activity
WHERE state = 'open' AND list_contains(labels, 'bug')
ORDER BY updated_at DESC
```

The complement — open issues no one has categorized yet — is often the better
triage list:

```sql
SELECT number, title, author, created_at
FROM issue_activity
WHERE state = 'open' AND len(labels) = 0
ORDER BY created_at ASC
```

### 7. Pull requests waiting on review

Non-draft open PRs with no inline review comments yet. `pending_reviewers`
lists whose review is still requested; review comments and conversation
comments are separate channels, so a PR with lively discussion can still be
unreviewed.

```sql
SELECT number, title, author, created_at, pending_reviewers, non_bot_comment_count
FROM pull_request_activity
WHERE state = 'open' AND NOT draft AND review_comment_count = 0
ORDER BY created_at ASC
```

### 8. Most-discussed open issues

High traffic with no resolution is a signal of contention or importance —
which one is for you to judge by reading the thread.

```sql
SELECT number, title, non_bot_comment_count, participant_count, last_comment_at
FROM issue_activity
WHERE state = 'open'
ORDER BY non_bot_comment_count DESC
LIMIT 15
```

### 9. Release notes: merged since a date

`merged_at` is NULL for anything not merged, so it does the filtering itself.
A closed PR with NULL `merged_at` was rejected, not shipped.

```sql
SELECT number, title, author, merged_at
FROM pull_request_activity
WHERE merged_at >= TIMESTAMP '2026-06-01'
ORDER BY merged_at ASC
```

To scope by release instead of date, get the previous release's date from the
local git history (`git log -1 --format=%cI <tag>`) and use that.

### 10. Search results filtered by facts

The score function works from any relation carrying the document `id`, so
search terms and derived facts combine in one query — here, searched threads
restricted to open, unanswered issues:

```sql
SELECT number, title, created_at FROM (
    SELECT *, fts_github_issue_threads.match_bm25(id, 'windows path separator') AS score
    FROM issue_activity
) WHERE score IS NOT NULL
  AND state = 'open'
  AND first_non_author_comment_at IS NULL
ORDER BY score DESC LIMIT 10
```

An id the index does not hold scores NULL (never an error), so mixing up an
issue index with a PR relation fails silently — check which entity your FROM
clause holds.

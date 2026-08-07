# Plan: Derived activity views with column documentation

Implements [#11](https://github.com/jayqi/ghtriage/issues/11). Lays the groundwork for
[#12](https://github.com/jayqi/ghtriage/issues/12) and includes the column-documentation
portion of it.

## Context

The largest gap between the raw dlt tables and real triage questions is that "when did anything
last happen on this issue?" requires joining `issues` against the comment tables and aggregating.
Every agent session re-derives the same joins.

This plan ships two SQL views, `github.issue_activity` and `github.pull_activity`, recreated on
every pull, plus `COMMENT ON` documentation for every column so the definitions are inspectable
through `ghtriage schema --table`.

Design principle carried over from the issue: **narrow, factual columns — no blended summary
columns and no judgments.** `last_comment_at` and `updated_at` are separate columns rather than a
single `last_activity_at` that bakes in a definition of "activity." "Stale" is an opinion the agent
forms by filtering on facts; the view only makes the facts cheap.

## Findings from the data

Investigated against a real pull (`drivendataorg/cloudpathlib`: 313 issues, 237 pulls,
1460 issue comments, 408 review comments) and DuckDB 1.4.4.

### 1. Half of `issue_comments` belongs to pull requests

GitHub's `/issues/comments` endpoint returns comments on both issues *and* pull requests.
`/pulls/comments` returns only inline diff review comments — not the PR conversation.

| Segment | Rows |
|---|---|
| `issue_comments` rows matching an issue number | 744 |
| `issue_comments` rows matching a pull request number | 716 |
| Orphans (matching neither) | 0 |

Consequence: **144 of 237 pull requests (61%) have conversation comments but zero review
comments.** A `pull_activity` exposing only `review_comment_count` would report those PRs as
completely silent. For another 48 PRs, the most recent activity is a conversation comment that a
review-only view would miss.

`pull_activity` therefore carries both channels as separate column groups —
`comment_count`/`first_comment_at`/`last_comment_at` for conversation, and
`review_comment_count`/`first_review_comment_at`/`last_review_comment_at` for inline review. This
is a deviation from the column list in #11, and it follows the issue's own principle: expose the
two facts separately rather than summing them into one "activity" number.

Issue and pull request numbers share one sequence per repository and never collide
(verified: zero overlap between `issues.number` and `pulls.number`), so number-based joins are
unambiguous in both directions.

### 2. Neither comment table has a join key column

`issue_comments` carries `issue_url` (`https://api.github.com/repos/OWNER/REPO/issues/550`) and
`pull_comments` carries `pull_request_url` (`.../pulls/22`). The number must be parsed out:

```sql
CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT)
```

This is the single most valuable thing the views encapsulate — non-obvious, and otherwise
rediscovered in every agent session.

### 3. `CREATE OR REPLACE VIEW` wipes comments

Verified on DuckDB 1.4.4: replacing a view silently drops its view-level and column-level comments.
Column documentation is therefore not one-time DDL — it must be reapplied immediately after every
view creation, on every pull. This is why #12's column-comment work belongs in this PR rather than
a follow-up: the mechanism has to exist alongside view creation regardless.

### 4. What `schema` already handles, and the one gap

| Code path | Source | Covers views? |
|---|---|---|
| `get_tables()` | `information_schema.tables` | Yes — views are listed with no change |
| `get_table_columns()` | `information_schema.columns` + `duckdb_columns()` | Yes — including column comments |
| `get_table_descriptions()` | `duckdb_tables()` | **No** — `duckdb_tables()` excludes views |

So `ghtriage schema --table issue_activity` works with zero changes. Only the description lookup in
the table listing needs a small fix, included here so a view's description is not silently
invisible. The rest of #12 (labeling rows `VIEW` vs `TABLE`, printing the view definition) stays in
#12.

### 5. `duckdb_views().sql` is normalized, not verbatim

DuckDB re-serializes the definition from its parse tree: keywords uppercase, formatting collapsed,
SQL comments stripped. This settles #12's "print verbatim or pretty-print?" open question — there is
no verbatim option unless the original text is stored separately, which this plan does not do.

### 6. Local counts can drift from GitHub's own

`issues.comments` is GitHub's server-side count; `comment_count` counts locally stored rows. They
agreed on all 250 commented issues in the sample, but merge-disposition loads never delete, so a
comment deleted on GitHub after being pulled leaves the two disagreeing. Both are facts; the column
comment says so and the raw column remains available.

### 7. The deprecated `assignee` scalar gives wrong answers

Pull request #542 has two assignees in `pulls__assignees` (`pjbull`, `Copilot`), but
`pulls.assignee__login` is `NULL`. GitHub deprecated the scalar `assignee` field in favor of the
`assignees` array, and it does not populate reliably when there is more than one. An agent writing
`WHERE assignee__login IS NULL` to find unassigned work would count #542 as unassigned.

This is a stronger case than mere join tedium: the obvious raw column is not just inconvenient, it
is misleading. `assignees` is aggregated from the child table exactly as `labels` is.

`pulls__requested_reviewers` gets the same treatment, named `pending_reviewers` — see the naming
note under the output spec.

### 8. There is no factual signal for "AI user"

Investigated for a possible `is_ai` column. What the data actually supports:

| Account | `user__type` | `[bot]` login suffix |
|---|---|---|
| `github-actions[bot]` | `Bot` | yes |
| `codecov[bot]` | `Bot` | yes |
| `dependabot[bot]` | `Bot` | yes |
| `Copilot` | `Bot` | **no** |
| `codecov-commenter` | **`User`** | no |

- The `[bot]` suffix heuristic is strictly dominated by `user__type` — it misses `Copilot`, and no
  account in the sample is suffixed while typed `User`.
- `user__type` has its own floor: `codecov-commenter` is a machine account GitHub types as `User`.
  `Bot` identifies GitHub Apps, not automation in general.
- `performed_via_github_app__slug` was evaluated as a third signal and rejected: populated on 129 of
  1460 comments, `issue_comments` only, and inconsistent within a single account (`codecov[bot]` has
  96 rows NULL and 67 with the slug).

Fundamentally, GitHub distinguishes Bot from User — not AI from CI. `github-actions[bot]` and
`Copilot` are both `Bot`. Separating them needs a maintained list of agent identities (`Copilot`,
`claude[bot]`, `devin-ai-integration[bot]`, and whatever a given repo self-hosts), which is a
judgment, goes stale, and is repo-specific. That is the same line the project already draws at
"stale."

Resolution: no AI column. Instead `author_type` is exposed as a pass-through of `user__type` on both
views, so `WHERE author_type = 'Bot'` works without dropping to raw tables, and an agent that wants
finer grain can pull distinct logins in one query and classify them itself.

### 9. Bot participation is large enough that unsplit counts mislead

64 of 313 issues (20%) and 331 of 1460 issue comments (23%) are Bot-authored. Four of the five most
recent issues are `github-actions[bot]` build failures, so any recency-ordered triage query returns
mostly automation unless it filters.

This makes the unsplit `comment_count` and `participant_count` non-neutral in a way that is easy to
miss: an issue whose only reply is a coverage bot reports `participant_count = 2` and a non-NULL
`first_non_author_comment_at`, reading as though someone responded. That is already an embedded
judgment — that bots count — just an invisible one. Ten issues here have no participant other than
bots.

The fix is not to filter bots out, which would be a judgment in the other direction. It is to split
the counts so the agent can go either way: `non_bot_comment_count`, `non_bot_review_comment_count`,
and `non_bot_participant_count` alongside their totals. See "Which quantity gets the unqualified
name" under the output spec for why the totals stay total and the companion counts non-bots rather
than bots.

**Bot comments segregate by channel**, which does useful work for free:

| Channel | Bot accounts |
|---|---|
| Conversation (`issue_comments`) | `github-actions[bot]` (168), `codecov[bot]` (163) |
| Inline review (`pull_comments`) | `Copilot` (48) — nothing else |

Status and coverage bots post to the conversation thread; code-review agents post inline. So on
`pull_activity`, the conversation-comment split is largely CI noise and the review-comment split is
largely AI review — the two land in different columns without ghtriage having to tell them apart.
This is a tendency, not a guarantee; a repo whose review agent posts conversation summaries would
blur it.

Copilot's review comments were checked against human reviewers before concluding they count as
meaningful activity rather than noise: 48 of 48 are anchored to a file and line, and it is the third
most prolific reviewer in the repo. Its 0/48 reply rate is below jayqi (35%) and pjbull (46%) but
not unlike klwetstone (6%), and all 48 comments fall on 4 pull requests over six months — too thin
to treat as a pattern. A bot review is meaningfully different from a human review, which is exactly
what the split column expresses without ghtriage ruling on which one matters.

**Accepted limitation:** these columns key on `user__type`, so machine accounts that are not GitHub
Apps — `codecov-commenter` here — count as non-bot. Accepted as edge-case degradation rather than
pursued with a curated list. The column comments state that the measure is account type, not
automation.

The non-bot filter is written `user__type IS DISTINCT FROM 'Bot'` rather than `<> 'Bot'`. No row in
the sample has a NULL `user__type`, but deleted accounts can produce one, and under `<>` a NULL
would fall into neither bucket — breaking the invariant that the bot count equals total minus
non-bot. `IS DISTINCT FROM` puts unknown-type accounts on the non-bot side, which is also the
conservative reading: an account is not asserted to be a bot without evidence.

## Output specification

### `github.issue_activity` — one row per issue

19 columns. Row count equals `issues` row count exactly (verified: 313 = 313).

| Column | Type | Definition (becomes the column comment) |
|---|---|---|
| `number` | `BIGINT` | Pass-through of `issues.number`. |
| `title` | `VARCHAR` | Pass-through of `issues.title`. |
| `state` | `VARCHAR` | Pass-through of `issues.state`. |
| `state_reason` | `VARCHAR` | Pass-through of `issues.state_reason`. |
| `author` | `VARCHAR` | Login of the issue opener. Pass-through of `issues.user__login`. |
| `author_type` | `VARCHAR` | GitHub account type of the issue opener: `User`, `Bot`, or `Organization`. Pass-through of `issues.user__type`. Machine accounts that are not GitHub Apps are typed `User`. |
| `labels` | `VARCHAR[]` | Sorted list of label names from `issues__labels`. Empty list when the issue has no labels. |
| `assignees` | `VARCHAR[]` | Sorted list of assignee logins from `issues__assignees`. Empty list when unassigned. Prefer this over the deprecated `issues.assignee__login`, which is unreliable when there is more than one assignee. |
| `created_at` | `TIMESTAMPTZ` | Pass-through of `issues.created_at`. |
| `updated_at` | `TIMESTAMPTZ` | Pass-through of `issues.updated_at`. GitHub bumps this for many edit types, not only comments. |
| `closed_at` | `TIMESTAMPTZ` | Pass-through of `issues.closed_at`. |
| `comment_count` | `BIGINT` | Count of `issue_comments` rows matching this issue number, including bot comments. May differ from `issues.comments` if comments were deleted on GitHub after being pulled. |
| `non_bot_comment_count` | `BIGINT` | Of `comment_count`, how many were posted by an account GitHub does not type as `Bot`. Subtract from `comment_count` for the bot count. Measures account type, not automation: machine accounts that are not GitHub Apps are typed `User` and counted here. |
| `first_comment_at` | `TIMESTAMPTZ` | Earliest `created_at` among matching `issue_comments` rows, including bot comments. NULL when there are none. |
| `last_comment_at` | `TIMESTAMPTZ` | Latest `created_at` among matching `issue_comments` rows, including bot comments. NULL when there are none. |
| `first_non_author_comment_at` | `TIMESTAMPTZ` | Earliest comment `created_at` whose author differs from the issue author, including bot comments. NULL when there is none. |
| `last_non_author_comment_at` | `TIMESTAMPTZ` | Latest comment `created_at` whose author differs from the issue author, including bot comments. NULL when there is none. |
| `participant_count` | `BIGINT` | Number of distinct logins in the set formed by the issue author together with all comment authors, including bots. NULL logins are not counted. |
| `non_bot_participant_count` | `BIGINT` | Of `participant_count`, how many are accounts GitHub does not type as `Bot`. Subtract from `participant_count` for the bot count. Excludes the issue author when the issue was opened by a bot. |

View comment: *Derived view: one row per issue with pre-joined comment activity, labels, and
assignees.*

### `github.pull_activity` — one row per pull request

23 columns. Row count equals `pulls` row count exactly (verified: 237 = 237).

| Column | Type | Definition (becomes the column comment) |
|---|---|---|
| `number` | `BIGINT` | Pass-through of `pulls.number`. |
| `title` | `VARCHAR` | Pass-through of `pulls.title`. |
| `state` | `VARCHAR` | Pass-through of `pulls.state`. Merged pull requests have state `closed`; use `merged_at` to distinguish. |
| `draft` | `BOOLEAN` | Pass-through of `pulls.draft`. |
| `author` | `VARCHAR` | Login of the pull request opener. Pass-through of `pulls.user__login`. |
| `author_type` | `VARCHAR` | GitHub account type of the pull request opener: `User`, `Bot`, or `Organization`. Pass-through of `pulls.user__type`. Machine accounts that are not GitHub Apps are typed `User`. |
| `labels` | `VARCHAR[]` | Sorted list of label names from `pulls__labels`. Empty list when the pull request has no labels. |
| `assignees` | `VARCHAR[]` | Sorted list of assignee logins from `pulls__assignees`. Empty list when unassigned. Prefer this over the deprecated `pulls.assignee__login`, which is unreliable when there is more than one assignee. |
| `pending_reviewers` | `VARCHAR[]` | Sorted list of logins with an outstanding review request, from `pulls__requested_reviewers`. GitHub drops a reviewer from this list once they submit a review, so it reflects unfulfilled requests only, including on closed pull requests. Empty list when there are none. |
| `created_at` | `TIMESTAMPTZ` | Pass-through of `pulls.created_at`. |
| `updated_at` | `TIMESTAMPTZ` | Pass-through of `pulls.updated_at`. |
| `closed_at` | `TIMESTAMPTZ` | Pass-through of `pulls.closed_at`. |
| `merged_at` | `TIMESTAMPTZ` | Pass-through of `pulls.merged_at`. NULL when the pull request was not merged. |
| `comment_count` | `BIGINT` | Count of conversation comments: `issue_comments` rows whose `issue_url` number matches this pull request, including bot comments. Excludes inline review comments. |
| `non_bot_comment_count` | `BIGINT` | Of `comment_count`, how many were posted by an account GitHub does not type as `Bot`. Subtract from `comment_count` for the bot count. Measures account type, not automation. |
| `first_comment_at` | `TIMESTAMPTZ` | Earliest `created_at` among matching conversation comments, including bot comments. NULL when there are none. |
| `last_comment_at` | `TIMESTAMPTZ` | Latest `created_at` among matching conversation comments, including bot comments. NULL when there are none. |
| `review_comment_count` | `BIGINT` | Count of inline review comments: `pull_comments` rows matching this pull request, including bot comments. Excludes conversation comments. |
| `non_bot_review_comment_count` | `BIGINT` | Of `review_comment_count`, how many were posted by an account GitHub does not type as `Bot`. Subtract from `review_comment_count` for the bot count. A bot review is meaningful activity but is not a human review; this column keeps the two distinguishable. |
| `first_review_comment_at` | `TIMESTAMPTZ` | Earliest `created_at` among matching review comments, including bot comments. NULL when there are none. |
| `last_review_comment_at` | `TIMESTAMPTZ` | Latest `created_at` among matching review comments, including bot comments. NULL when there are none. |
| `participant_count` | `BIGINT` | Number of distinct logins in the set formed by the pull request author together with all conversation- and review-comment authors, including bots. NULL logins are not counted. |
| `non_bot_participant_count` | `BIGINT` | Of `participant_count`, how many are accounts GitHub does not type as `Bot`. Subtract from `participant_count` for the bot count. Excludes the pull request author when it was opened by a bot. |

View comment: *Derived view: one row per pull request with pre-joined conversation-comment,
review-comment, label, assignee, and review-request facts.*

#### Which quantity gets the unqualified name

Every split measure is carried as a **total** plus a **non-bot** companion, never as a non-bot base.

The totals stay total for two reasons. `issues.comments` — GitHub's own server-side total — sits in
the raw table under a near-identical name, and a `comment_count` that quietly meant something
narrower would be a worse trap than any arithmetic error. More fundamentally, excluding bots from
the unqualified name *is* the judgment: "bot comments aren't comments" is a decision, and the
unqualified name should carry the unqualified quantity or there is no column for "how many comments
are on this" at all.

The companion counts non-bots rather than bots because of how badly the default fails on pull
requests. 196 of 237 pull requests (83%) carry bot conversation comments, and on 75 of them (32%)
*every* comment is a bot. An agent writing `WHERE comment_count = 0` to find undiscussed pull
requests silently misses all 75 — a false negative that never surfaces. Making the non-bot count
directly available turns that into `WHERE non_bot_comment_count = 0` with no arithmetic. The
quantity that now requires subtraction is the bot-only count, which is rarely wanted on its own.

`non_bot_` over `human_`: the column measures `user__type IS DISTINCT FROM 'Bot'`, and `human_`
would overclaim given `codecov-commenter`. It also matches the `non_author_` prefix already used by
the timestamp columns.

#### Why `pending_reviewers` and not `requested_reviewers`

GitHub's own field name is `requested_reviewers`, and every other pass-through in these views keeps
its upstream name. This one is renamed deliberately, because the upstream name invites the wrong
reading — "everyone who was asked to review" — when the field actually holds only *unfulfilled*
requests. Encoding the semantics in the name is better than shipping a misleading name and relying
on the doc string to walk it back. The column comment names the source table so the mapping back to
raw data stays obvious.

`pending_reviews` was considered and rejected: the column holds logins, not reviews, and the `-ers`
keeps the type legible.

Note that "pending" persists past close — all 20 PRs with review requests in the sample are closed,
because a request that is never fulfilled is never cleared. The column comment states this.

### `ghtriage schema` listing

Views appear alongside tables, sorted by name, each with its description:

```
table                                             | description
--------------------------------------------------+---------------------------------------------------------------
issue_activity                                    | Derived view: one row per issue with pre-joined comment ...
issue_comments                                    | Comments provide a way for people to collaborate on an issue.
issues                                            | Issues are a great way to keep track of tasks, enhancements ...
issues__labels                                    |
pull_activity                                     | Derived view: one row per pull request with pre-joined ...
pull_comments                                     | Pull Request Review Comments are comments on a portion of ...
pulls                                             |
```

The `Derived view:` prefix is the interim signal that a row is not raw data. #12 replaces it with a
proper type column.

### `ghtriage schema --table issue_activity`

Rendered through the existing `_format_table` path (real output, abbreviated to the derived
columns):

```
column_name                 | data_type                | nullable | description
----------------------------+--------------------------+----------+--------------------------------------------------
author_type                 | VARCHAR                  | True     | GitHub account type of the issue opener: User, ...
labels                      | VARCHAR[]                | True     | Sorted list of label names from issues__labels. ...
assignees                   | VARCHAR[]                | True     | Sorted list of assignee logins from issues__ass ...
comment_count               | BIGINT                   | True     | Count of issue_comments rows matching this issue ...
non_bot_comment_count       | BIGINT                   | True     | Of comment_count, how many were posted by an acc ...
first_comment_at            | TIMESTAMP WITH TIME ZONE | True     | Earliest created_at among matching issue_comments ...
last_comment_at             | TIMESTAMP WITH TIME ZONE | True     | Latest created_at among matching issue_comments ...
first_non_author_comment_at | TIMESTAMP WITH TIME ZONE | True     | Earliest comment created_at whose author differs ...
last_non_author_comment_at  | TIMESTAMP WITH TIME ZONE | True     | Latest comment created_at whose author differs ...
participant_count           | BIGINT                   | True     | Number of distinct logins in the set formed by t ...
non_bot_participant_count   | BIGINT                   | True     | Of participant_count, how many are accounts GitH ...
```

All columns report `nullable = True` because DuckDB does not infer non-nullability through a view.

### `labels` rendering across output formats

`VARCHAR[]` is a DuckDB LIST — a real nested value, not a delimited string.

| `--format` | Rendering |
|---|---|
| `json` | `{"number": 64, "labels": ["S3", "bug", "design decision", "good first issue"]}` — proper JSON array |
| `table` | `['S3', 'bug', 'design decision', 'good first issue']` — Python `repr` |
| `csv` | `64,"['S3', 'bug', 'design decision', 'good first issue']"` — Python `repr`, CSV-quoted |

`table` and `csv` leak Python's `repr` because `_format_table` and `_format_csv` stringify values
directly. This is pre-existing behavior for any nested type; `labels` is just the first column to
exercise it. Left alone deliberately — the list form is what makes
`WHERE list_contains(labels, 'bug')` work, and an agent can flatten with
`array_to_string(labels, ', ')` when it wants text. Changing the formatters is a separate decision.

### Example queries the views enable

```sql
-- Open issues with no reply from anyone but the author
SELECT number, title, created_at
FROM issue_activity
WHERE state = 'open' AND first_non_author_comment_at IS NULL
ORDER BY created_at;

-- Open PRs ordered by how long since anything happened on them
SELECT number, title, author, greatest(coalesce(last_comment_at, created_at),
                                       coalesce(last_review_comment_at, created_at)) AS last_touch
FROM pull_activity
WHERE state = 'open' AND NOT draft
ORDER BY last_touch;

-- Label co-occurrence on open issues
-- (unnest must be in a subquery or lateral join; DuckDB rejects it beside an aggregate)
SELECT label, count(*) AS n
FROM (SELECT unnest(labels) AS label FROM issue_activity WHERE state = 'open')
GROUP BY 1 ORDER BY n DESC;

-- Unassigned open issues not opened by a bot account, oldest first
SELECT number, title, author, created_at
FROM issue_activity
WHERE state = 'open' AND author_type = 'User' AND len(assignees) = 0
ORDER BY created_at;

-- Open PRs still waiting on a review request
SELECT number, title, author, pending_reviewers, created_at
FROM pull_activity
WHERE state = 'open' AND len(pending_reviewers) > 0
ORDER BY created_at;

-- Open PRs nobody has actually discussed (bot comments do not count as discussion here)
SELECT number, title, author, created_at
FROM pull_activity
WHERE state = 'open' AND non_bot_comment_count = 0
ORDER BY created_at;

-- PRs reviewed by a bot but by no human
SELECT number, title, author, review_comment_count
FROM pull_activity
WHERE review_comment_count > 0 AND non_bot_review_comment_count = 0;

-- Issues where nobody but bots has participated
SELECT number, title, created_at
FROM issue_activity
WHERE non_bot_participant_count = 0
ORDER BY created_at DESC;
```

## Implementation

### 1. New module: `src/ghtriage/views.py`

Holds the view SQL, the column documentation, and the creation logic.

**Design principle: the views are SQL, written as SQL.** Each view is one template string that reads
top-to-bottom as a query — greppable, and pasteable straight into `ghtriage query` for debugging.
There is no expression-composition layer. Degradation is handled by two narrow mechanisms that are
themselves mostly SQL, described below.

```python
ISSUE_ACTIVITY_SQL = r"""
WITH issues_padded AS (
    SELECT * FROM github.issues
    UNION ALL BY NAME
    SELECT NULL::VARCHAR AS state_reason,
           NULL::TIMESTAMP WITH TIME ZONE AS closed_at
    WHERE false
),
comments_keyed AS (
    SELECT CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS issue_number,
           user__login AS login, user__type AS utype, created_at
    FROM {issue_comments}
),
...
"""
```

**Missing columns: `UNION ALL BY NAME`.** Each base table gets a `_padded` CTE that unions it with a
zero-row relation declaring the columns dlt may not have created yet, with their types. DuckDB
aligns by name, so a column the real table already has keeps its real values, and one it lacks
arrives as a typed NULL. Verified: identical 19-column output from a table with and without
`closed_at` / `state_reason`. This replaces per-column degradation logic with four lines of SQL.

**The padding templates must match dlt's types exactly.** `UNION ALL BY NAME` does not error on a
type mismatch — it silently coerces to a common type, and the padding relation is unioned in on
*every* pull, not only when the column is absent. A template declaring `TIMESTAMP WITH TIME ZONE`
against a table holding naive `TIMESTAMP` rewrites every value by assuming the machine's local
timezone; `VARCHAR` against `BIGINT` turns `42` into `'42'`. Both were verified to pass silently.

Two mitigations, both required:

- Keep padding entries to columns that can genuinely be absent — `state_reason`, `closed_at`,
  `merged_at`, `draft`. Every additional entry is another chance to mistype a type.
- `test_view_column_types_match_spec` asserts the created views' column types against the types in
  this document, so a mismatch fails in CI rather than corrupting timestamps on someone's machine.

**Missing tables: one `{slot}` per optional source.** Child tables and comment tables only exist once
a record carrying that field has been pulled — for assignees and review requests, possibly never.
Each is a format slot filled with either `github.<table>` or a typed empty relation:

```python
EMPTY = {
    "issues__assignees": "(SELECT NULL::VARCHAR AS _dlt_parent_id, NULL::VARCHAR AS login WHERE false)",
    ...
}
```

Seven distinct slots, appearing eight times across the two views — `issue_comments` is used by
both. The full set: `issue_comments`, `pull_comments`, `issues__labels`, `issues__assignees`,
`pulls__labels`, `pulls__assignees`, `pulls__requested_reviewers`. Rendering is one `str.format`
call against the set of tables present in the `github` schema, read once.

**Missing base table** (`issues` / `pulls`) skips that view entirely — the only case that does.

**Column documentation lives in a separate dict**, mirroring the shape `annotations.py` already uses
for the OpenAPI descriptions:

```python
VIEW_COLUMN_DOCS: dict[str, dict[str, str]] = {"issue_activity": {...}, "pull_activity": {...}}
VIEW_DOCS: dict[str, str] = {...}
```

Docs and SQL are deliberately *not* colocated. Colocation was considered and rejected: it requires a
record-per-column composition layer to build the SQL, and it does not actually enforce anything —
someone editing an expression can leave the adjacent doc stale just as easily. The enforceable
property comes from a test instead (see below), which is strictly stronger because it catches drift
in both directions. Keeping the docs in a dict also means one documentation pattern in the codebase
rather than two.

**`create_views(db_path: Path) -> None`**

Opens one connection, reads the set of present tables once, then for each view:

1. Skip if the base table is absent.
2. `CREATE OR REPLACE VIEW github.<name> AS <rendered sql>`
3. `COMMENT ON VIEW github.<name> IS '<doc>'`
4. `COMMENT ON COLUMN github.<name>.<col> IS '<doc>'` for every entry in the docs dict

Steps 3–4 must follow step 2 every time — `CREATE OR REPLACE` drops comments (finding 3). Single
quotes escaped as `''`; DDL does not take bound parameters, matching `annotations.py`.

Columns that degraded to NULL still get their comments: the definition is unchanged, the upstream
data simply has not produced values yet, and an undocumented NULL column is less legible, not more.

Each view is wrapped individually in try/except: a failure warns to stderr and moves to the next.
A view failure never fails the pull, matching `fetch_and_annotate`'s posture.

**Drift protection.** `test_view_docs_match_view_columns` creates both views against a temp database
and asserts the docs dict keys are exactly equal to the view's actual column set. A column added to
the SQL without a doc fails; a doc left behind for a removed column fails. This is the guarantee the
colocated-record design was reaching for, obtained from a single test.

#### Two SQL details that are load-bearing

**Child-table aggregation happens before the join.** `labels`, `assignees`, and `pending_reviewers`
each aggregate in their own CTE keyed on `_dlt_parent_id`, then `LEFT JOIN` to the base table on
`_dlt_id`. Joining first and aggregating after would multiply the list columns against each other.
Verified row parity holds at 237 = 237 with all three joined.

**Participants are computed as a set, not as a count plus one.** The obvious form —
`COUNT(DISTINCT commenter) FILTER (WHERE commenter <> author) + 1` — cannot produce a non-bot count
that subtracts correctly, because the total and the split have to be drawn from the same set. So
participants are built explicitly:

```sql
participants AS (
    SELECT i.number AS issue_number, i.user__login AS login, i.user__type AS utype
    FROM issues_padded i
    UNION
    SELECT c.issue_number, c.login, c.utype FROM comments_keyed c
),
participant_agg AS (
    SELECT issue_number,
           COUNT(DISTINCT login) AS participant_count,
           COUNT(DISTINCT login) FILTER (WHERE utype IS DISTINCT FROM 'Bot')
               AS non_bot_participant_count
    FROM participants GROUP BY issue_number
)
```

This matters most when the item was opened by a bot, true of 64 issues in the sample. It also fixes
a latent bug in the `+ 1` form, which credits a participant even when the author login is NULL
(deleted account), contradicting the documented "NULL logins are not counted" behavior.

Verified behavior-preserving: the set form and the `+ 1` form agree on all 313 issues in the sample,
and the split never exceeds the total.

#### Views, not materialized tables

Recomputed on every query rather than stored. Measured on the sample repo (313 issues, 237 pulls,
1868 comments): a full scan of **both** views returns in 38 ms. Since agents filter rather than scan
whole views, real query times are lower still.

Views also stay correct by construction — a table would need invalidating whenever a pull changed
the underlying data, which is exactly the staleness class the project already fights with the
snapshot model. Revisit only if a repository appears where scans are slow enough to notice; the
change would be `CREATE TABLE AS` in `create_views` with no change to the SQL or the docs.

#### Interactions checked

- **`ghtriage status` is unaffected.** `_MAIN_TABLES` in `src/ghtriage/query.py` is a
  fixed tuple of the four raw tables, so views do not appear in the status summary. That is correct
  — status reports what was pulled and how fresh it is, and a view has no independent freshness.
- **`ghtriage query` still works read-only.** Views are stored objects; selecting from them needs no
  write access, so [#8](https://github.com/jayqi/ghtriage/issues/8)'s read-only connection is
  preserved. Verified.
- **Name collision with a dlt table.** If dlt ever created a table named `issue_activity`,
  `CREATE OR REPLACE VIEW` fails with an object-type error rather than clobbering it. The per-view
  try/except turns that into a warning. dlt names tables after resources and their nested fields, so
  this cannot happen with the current source.
- **Existing databases need no migration.** Views appear on the next `pull`; nothing reads them
  before they exist.

### 2. `src/ghtriage/pipeline.py`

In `run_pull()`, between `_write_meta()` and `fetch_and_annotate()`:

```python
create_views(db_path)
fetch_and_annotate(db_path)
```

Creation happens on pull, not on query: [#8](https://github.com/jayqi/ghtriage/issues/8) made
`execute_query` open the connection read-only, so view creation at query time is not possible
without giving that up. On-pull also survives `--full`, which deletes the database outright and
rebuilds it.

Ordering note: `create_views` runs before `fetch_and_annotate` so that the OpenAPI annotation pass
sees the views already present. `annotate_database` filters to its four known raw tables, so it will
not touch view comments either way, but the ordering keeps the "database is fully assembled, then
annotated" sequence intact.

### 3. `src/ghtriage/query.py`

`get_table_descriptions()` currently reads only `duckdb_tables()`, which excludes views. Union in
`duckdb_views()`:

```sql
SELECT table_name, comment FROM duckdb_tables() WHERE schema_name = 'github'
UNION ALL
SELECT view_name, comment FROM duckdb_views() WHERE schema_name = 'github'
```

No other query change is needed — `get_tables()` and `get_table_columns()` already cover views.

### 4. `src/ghtriage/cli.py`

No changes. The existing `schema` handlers render views correctly once the description lookup is
fixed.

### 5. `README.md`

Add a short subsection under "How it works" describing the two derived views, why the two comment
channels on `pull_activity` are separate, and that column definitions are inspectable via
`ghtriage schema --table`. Add one view-based example to the Examples block.

### 6. New `docs/decisions.md`

**Why it exists.** This plan carries the reasoning behind a dozen non-obvious schema choices, but
`docs/plans/` archives implemented plans to `docs/plans/archive/`. Plans describe work to be done
and go stale on completion; decisions like "excluding bots from the unqualified name is itself a
judgment" stay load-bearing indefinitely. Different lifecycle, so a different document — otherwise
the reasoning ends up somewhere whose directory name signals "historical."

**Relationship to the column comments.** No conflict with
[#12](https://github.com/jayqi/ghtriage/issues/12)'s "no prose documentation files — the database
itself is the documentation surface." Column comments answer *what a column means* for an agent
querying the data. This answers *why the schema has this shape* for someone modifying ghtriage.
Different audiences; the second is AGENTS.md's, which is why AGENTS.md gets a pointer.

**Inclusion criterion — the thing that keeps it short enough to survive:** record a decision only
when a future reader might reasonably undo it. Findings are not decisions. That 49% of
`issue_comments` belong to pull requests is a fact, and it belongs in the plan and the column
comments; that the two channels are kept separate rather than summed is a decision, and it belongs
here.

**Format.** One append-only file. Per entry: the decision, the rejected alternative, and a link to
the plan section carrying the full reasoning. Three to four lines. Deliberately not ADR-per-file —
that ceremony is what kills decision logs on a project this size. Linking rather than restating also
keeps the log from drifting out of sync with the detailed rationale.

**Link stability.** Entries cite the issue number and the plan *filename*, not a path — this plan
moves to `docs/plans/archive/` once implemented, and a path-based link would break on the very move
the log exists to survive. The issue number is permanent; the filename is greppable from either
directory.

**Seed entries, from this plan:**

| Decision | Rejected alternative |
|---|---|
| Split counts carry the total under the unqualified name, with a `non_bot_` companion | Making the unqualified name mean the non-bot count |
| No AI-identification column; `author_type` is as far as the data supports | A curated list of agent logins |
| `pending_reviewers` rather than GitHub's `requested_reviewers` | Preserving the upstream field name |
| Views created on pull, not on first query | Creating on query, which #8's read-only connection forbids |
| Pull request conversation and review comments stay separate | A single combined comment count |
| `labels` / `assignees` / `pending_reviewers` are `VARCHAR[]` | A delimited string that renders uniformly across output formats |
| Views degrade to NULL columns and empty relations when *optional* upstream data is absent | Skipping view creation whenever anything is missing |
| Views are plain SQL templates with format slots; docs live in a separate dict | A record-per-column composition layer that colocates SQL and docs |
| Views, not materialized tables | `CREATE TABLE AS`, which would need invalidation on every pull |

### 7. `AGENTS.md`

Add a pointer to `docs/decisions.md` under a short "Design decisions" heading, so an agent working
on ghtriage itself finds the rationale before proposing to change a schema choice.

## Tests

**New `tests/test_views.py`:**

- `test_create_views_creates_issue_activity` — the view exists with its pass-through columns
- `test_create_views_skips_view_when_base_table_missing` — no `issues` table, no `issue_activity`,
  no exception
- `test_create_views_pads_missing_column_with_typed_null` — `issues` without `state_reason` still
  yields a view with a `state_reason` column, via `UNION ALL BY NAME`
- `test_create_views_column_set_identical_with_and_without_optional_sources` — the degraded and
  fully-populated databases produce the same 19/23 columns in the same order, so a query written
  against one works against the other
- `test_create_views_substitutes_empty_relation_for_missing_source_table` — no `issue_comments`
  table → view creates, `comment_count` is 0, timestamps NULL
- `test_create_views_padding_preserves_real_values` — a column that *does* exist is not blanked by
  its padding entry
- `test_create_views_issue_activity_row_count_matches_issues` — real temp DuckDB
- `test_create_views_issue_comment_aggregates` — fixture with known comments verifies
  `comment_count`, `first_comment_at`, `last_comment_at`
- `test_create_views_non_author_columns_exclude_author_comments` — author-only thread leaves
  `first_non_author_comment_at` NULL
- `test_create_views_participant_count_counts_author_once` — author who also comments counts once
- `test_create_views_participant_count_ignores_null_author`
- `test_create_views_non_bot_comment_count_splits_by_user_type`
- `test_create_views_non_bot_comment_count_counts_non_app_machine_account` — a `User`-typed machine
  account counts as non-bot, pinning the accepted limitation from finding 9
- `test_create_views_non_bot_comment_count_counts_null_user_type_as_non_bot` — guards the
  `IS DISTINCT FROM` filter; under `<>` such a row would land in neither bucket
- `test_create_views_non_bot_participant_count_subtracts_exactly` — total minus non-bot equals the
  distinct bot logins, including the case where the item was opened by a bot
- `test_create_views_non_bot_review_comment_count_splits_by_user_type`
- `test_create_views_non_bot_counts_equal_totals_when_no_bots_present`
- `test_create_views_pull_activity_separates_conversation_and_review_comments` — the finding-1
  regression guard: a PR with conversation comments and no review comments reports
  `comment_count > 0`, `review_comment_count = 0`
- `test_create_views_pull_participant_count_spans_both_comment_tables`
- `test_create_views_labels_sorted_and_empty_list_when_none`
- `test_create_views_assignees_sorted_and_empty_list_when_none`
- `test_create_views_assignees_includes_all_when_scalar_assignee_is_null` — the finding-7 regression
  guard: a record with two assignees and a NULL `assignee__login` yields both logins
- `test_create_views_pending_reviewers_sorted_and_empty_list_when_none`
- `test_create_views_multiple_list_columns_do_not_fan_out` — a pull request with labels, assignees,
  and pending reviewers all populated still produces exactly one row
- `test_create_views_author_type_passes_through`
- `test_create_views_applies_view_and_column_comments` — verify via `duckdb_views()` /
  `duckdb_columns()`
- `test_create_views_reapplies_comments_after_replace` — run `create_views` twice, comments still
  present (guards finding 3)
- `test_create_views_swallows_errors` — a broken definition warns without raising
- `test_view_docs_match_view_columns` — the docs dict keys equal the created view's column set
  exactly, in both directions; this is the drift guard that replaces colocating docs with SQL
- `test_every_format_slot_has_an_empty_relation` — every `{slot}` in the SQL templates has a
  corresponding `EMPTY` entry, so rendering can never raise `KeyError` on a sparse repo
- `test_view_column_types_match_spec` — created view column types match the types documented in the
  output specification, catching a mistyped padding entry before it silently coerces real data
- `test_padding_does_not_coerce_existing_column_type` — a populated `closed_at` keeps its type and
  values after the padding union
- `test_create_views_is_idempotent` — running twice produces identical column sets, types, comments,
  and row counts

**Modify `tests/test_pipeline.py`:** mock `ghtriage.pipeline.create_views`; verify it is called once
after `pipeline.run()` and before `fetch_and_annotate`, in both full and non-full cases.

**Modify `tests/test_query.py`:** add a view to the `sample_cwd` fixture; verify `get_tables()`
includes it and `get_table_descriptions()` returns its comment.

**Modify `tests/test_cli.py`:** verify `schema` lists a view with its description, and
`schema --table <view>` prints the description column.

## Files changed

| File | Change |
|---|---|
| `src/ghtriage/views.py` | New — view definitions, column docs, `create_views` |
| `src/ghtriage/pipeline.py` | Call `create_views(db_path)` in `run_pull` before `fetch_and_annotate` |
| `src/ghtriage/query.py` | `get_table_descriptions()` unions `duckdb_views()` |
| `README.md` | Document the derived views; add a view-based example |
| `docs/decisions.md` | New — append-only decision log, seeded from this plan |
| `AGENTS.md` | Pointer to `docs/decisions.md` |
| `tests/test_views.py` | New |
| `tests/test_pipeline.py` | Mock and assert `create_views` |
| `tests/test_query.py` | View coverage for `get_tables` / `get_table_descriptions` |
| `tests/test_cli.py` | View coverage for `schema` output |

## Verification

```bash
just lint
just test
```

Manual end-to-end against a real repository (requires `GITHUB_TOKEN`):

```bash
ghtriage pull --full
ghtriage schema
ghtriage schema --table issue_activity
ghtriage schema --table pull_activity
ghtriage query "SELECT count(*) FROM issue_activity" --format json
ghtriage query "SELECT number, comment_count, review_comment_count FROM pull_activity WHERE state='open' ORDER BY number DESC LIMIT 10"
ghtriage pull    # incremental; confirm comments survive view recreation
ghtriage schema --table issue_activity
```

## Implementation sequence

Red/green TDD per `AGENTS.md`: write the failing test first, watch it fail **for the
reason you expect**, then write the minimum code that makes it pass. A test that passes the moment
you write it, or fails with an import error when you meant to assert on behavior, has not tested
anything.

Run `just test` after each step and `just lint` before each commit. `just test` is variadic — use
`just test tests/test_views.py -k <pattern>` for a tight inner loop, and a full run before moving to
the next step.

Steps 1–8 build `issue_activity` completely, including both degradation paths. `pull_activity` in
step 9 is then largely mechanical, and re-runs the same degradation suite against its own structure
rather than assuming it transfers.

### Step 1 — Fixtures and a view that exists

**Red.** `test_create_views_creates_issue_activity` — `create_views()` against a temp database
produces a `github.issue_activity` view whose pass-through columns are `number`, `title`, `state`,
`state_reason`, `author`, `author_type`, `created_at`, `updated_at`, `closed_at`. Add
`test_create_views_author_type_passes_through` alongside it.

Build the shared fixture here: a temp DuckDB with `github.issues`, `github.issue_comments`, and the
child tables, populated with a small hand-written dataset that exercises the cases the later steps
assert on — a bot-authored issue, an author who also comments, a bot commenter, a multi-assignee
record, and an issue with no comments at all. Getting this dataset right once saves rewriting
fixtures in every later step.

**Green.** `src/ghtriage/views.py` with `ISSUE_ACTIVITY_SQL` (pass-through projection only),
`VIEWS`, `BASE_TABLES`, and a `create_views(db_path)` that renders and executes.

### Step 2 — Documentation machinery and the drift guard

**Red.** `test_create_views_applies_view_and_column_comments` (verify through `duckdb_views()` and
`duckdb_columns()`) and `test_view_docs_match_view_columns` (docs dict keys equal the view's actual
column set, both directions).

**Green.** `VIEW_DOCS`, `VIEW_COLUMN_DOCS`, and the `COMMENT ON` pass in `create_views`, with `'`
escaped as `''`.

Doing this second is deliberate: from here on, the drift guard fails the suite whenever a column is
added without a doc, so every later step is forced to ship documentation with its columns rather
than leaving it for a cleanup pass.

### Step 3 — Comment aggregates

**Red.** `test_create_views_issue_comment_aggregates`, `test_create_views_issue_activity_row_count_matches_issues`.

**Green.** `comments_keyed` and `comment_agg` CTEs; `comment_count`, `first_comment_at`,
`last_comment_at` projected, with docs.

Assert the `regexp_extract` join key on a comment whose `issue_url` ends in a multi-digit number —
that expression is the single most load-bearing line in the view.

### Step 4 — Non-author timestamps

**Red.** `test_create_views_non_author_columns_exclude_author_comments` — an author-only thread
leaves `first_non_author_comment_at` NULL.

**Green.** The two `FILTER (WHERE login IS DISTINCT FROM ...)` aggregates, with docs.

### Step 5 — Participants and the non-bot splits

**Red.** `test_create_views_participant_count_counts_author_once`,
`test_create_views_participant_count_ignores_null_author`,
`test_create_views_non_bot_comment_count_splits_by_user_type`,
`test_create_views_non_bot_participant_count_subtracts_exactly` (including a bot-authored issue),
`test_create_views_non_bot_comment_count_counts_null_user_type_as_non_bot`,
`test_create_views_non_bot_comment_count_counts_non_app_machine_account`,
`test_create_views_non_bot_counts_equal_totals_when_no_bots_present`.

**Green.** The `participants` / `participant_agg` CTEs in set form, and
`FILTER (WHERE utype IS DISTINCT FROM 'Bot')` on the count columns.

Write the NULL-`user__type` test before the implementation and confirm it fails under a naive
`<> 'Bot'`. If it passes with `<>`, the test is not reaching the case it claims to.

### Step 6 — List columns

**Red.** `test_create_views_labels_sorted_and_empty_list_when_none`,
`test_create_views_assignees_sorted_and_empty_list_when_none`,
`test_create_views_assignees_includes_all_when_scalar_assignee_is_null`,
`test_create_views_multiple_list_columns_do_not_fan_out`.

**Green.** `label_agg` and `assignee_agg` CTEs, each aggregating before the join, with
`COALESCE(..., CAST([] AS VARCHAR[]))`.

The fan-out test must fail if the aggregation is moved after the join — verify by temporarily
joining the child tables directly.

### Step 7 — Degradation: missing source tables

**Red.** `test_create_views_skips_view_when_base_table_missing`,
`test_create_views_substitutes_empty_relation_for_missing_source_table`,
`test_every_format_slot_has_an_empty_relation`.

**Green.** `{slot}` placeholders, the `EMPTY` dict, the present-tables lookup, the base-table skip,
and the per-view try/except.

`test_create_views_swallows_errors` belongs here too — a deliberately broken definition must warn to
stderr without raising, and must not prevent the other view from being created.

### Step 8 — Degradation: missing columns

**Red.** `test_create_views_pads_missing_column_with_typed_null`,
`test_create_views_padding_preserves_real_values`,
`test_create_views_column_set_identical_with_and_without_optional_sources`,
`test_view_column_types_match_spec`,
`test_padding_does_not_coerce_existing_column_type`.

**Green.** The `issues_padded` CTE with `UNION ALL BY NAME`.

The last two tests are the guard against the silent-coercion hazard. Write them against a fixture
whose `closed_at` is populated and correctly typed, and confirm they go red if the padding entry's
type is changed to something coercible — that is the failure this pair exists to catch.

### Step 9 — `pull_activity`

**Red.** `test_create_views_pull_activity_separates_conversation_and_review_comments` (the finding-1
regression guard), `test_create_views_pull_participant_count_spans_both_comment_tables`,
`test_create_views_non_bot_review_comment_count_splits_by_user_type`,
`test_create_views_pending_reviewers_sorted_and_empty_list_when_none`, plus the step 7 and 8
degradation tests re-pointed at `pull_activity`.

**Green.** `PULL_ACTIVITY_SQL` and its docs, its three additional `EMPTY` slots, and the three-way
`participants` union.

Do not assume the degradation work transfers. `pull_activity` unions three sources into
`participants` and pads three columns rather than two.

### Step 10 — Pipeline integration

**Red.** In `tests/test_pipeline.py`, mock `ghtriage.pipeline.create_views` and assert it is called
once after `pipeline.run()` and before `fetch_and_annotate`, for both `full=True` and `full=False`.

**Green.** The `create_views(db_path)` call in `run_pull`.

Also add `test_create_views_is_idempotent` and `test_create_views_reapplies_comments_after_replace`
here — running `create_views` twice must leave identical columns, types, comments, and row counts.
The second is the finding-3 guard and only has meaning once the full pipeline path is wired.

### Step 11 — `schema` surfaces view descriptions

**Red.** In `tests/test_query.py`, add a view to the `sample_cwd` fixture and assert
`get_table_descriptions()` returns its comment; in `tests/test_cli.py`, assert `schema` lists the
view with its description and `schema --table <view>` prints the description column.

**Green.** Union `duckdb_views()` into the `get_table_descriptions()` query.

### Step 12 — Documentation

No tests. `README.md` view subsection and example, new `docs/decisions.md` seeded with the entries
above, and the `AGENTS.md` pointer. Then the full manual end-to-end from the Verification section,
including one run against a repository that has never used assignees or review requests — the
degradation paths are unit-tested, but a real sparse repository is the honest check.

## Implementation checklist

**Code**

- [x] Step 1 — fixtures; `views.py` with `ISSUE_ACTIVITY_SQL` pass-through columns and `create_views`
- [x] Step 2 — `VIEW_DOCS` / `VIEW_COLUMN_DOCS`, `COMMENT ON` pass, drift guard green
- [x] Step 3 — `comments_keyed` / `comment_agg`; count and timestamp columns
- [x] Step 4 — non-author timestamp columns
- [x] Step 5 — set-form `participants`; all non-bot split columns
- [x] Step 6 — `label_agg` / `assignee_agg`; list columns with empty-list defaults
- [x] Step 7 — `{slot}` rendering, `EMPTY` dict, base-table skip, per-view try/except
- [x] Step 8 — `UNION ALL BY NAME` padding; type-coercion guards
- [x] Step 9 — `PULL_ACTIVITY_SQL`, docs, slots, three-way participant union
- [x] Step 10 — `run_pull` calls `create_views` before `fetch_and_annotate`
- [x] Step 11 — `get_table_descriptions()` unions `duckdb_views()`

**Specification conformance**

- [x] `issue_activity` has exactly the 19 columns in the output spec, in order
- [x] `pull_activity` has exactly the 23 columns in the output spec, in order
- [x] Every column's type matches the spec
- [x] Every column carries a `COMMENT ON COLUMN`; both views carry a `COMMENT ON VIEW`
- [x] Row counts equal the base tables exactly
- [x] No `non_bot_*` column ever exceeds its total

**Degradation**

- [x] Missing base table skips only that view, with a stderr warning and no exception
- [x] Missing comment or child tables still produce the full column set
- [x] Missing optional columns still produce the full column set, typed
- [x] Column sets are identical between a sparse and a fully populated database

**Documentation**

- [x] `README.md` — derived-views subsection and a view-based example
- [x] `docs/decisions.md` — created and seeded
- [x] `AGENTS.md` — pointer to the decision log

**Final**

- [x] `just lint` clean
- [x] `just test` fully green
- [x] Manual end-to-end on a real repository, including a second incremental `pull`
- [x] Manual check on a repository that has never used assignees or review requests
- [x] This plan moved to `docs/plans/archive/`

## Deferred

- **AI-agent identification, distinct from bot identification.** Findings 8 and 9 take this as far
  as the data supports: `author_type` plus the three bot-split counts. Telling an AI agent from CI
  automation still needs a curated list of agent logins, which is a judgment the project's design
  principle keeps out of the database. If it is wanted later, the natural home is a user-maintained
  set in `.ghtriage/config.toml` — committable, repo-specific, and owned by the user rather than by
  ghtriage. Worth revisiting once there is real usage showing what the bot split fails to answer.
- **Non-author comment columns on `pull_activity`.** "Has anyone but the author engaged with this
  PR?" is a strong triage question, but per-channel columns would add four more. Left out to match
  #11's column list; easy to add later.
- **Account type for assignees and reviewers.** `assignees` and `pending_reviewers` carry logins
  only, though the child tables also have a `type` column. "Which of my assignees is a bot" seems
  rare enough not to justify parallel list columns; the child-table join remains available.
- **Bot-split timestamp columns.** The counts are split but the timestamps are not, so
  `last_review_comment_at` may be a bot's and "when did a human last review this" is not answerable
  from the view alone. Splitting every timestamp would roughly double the activity columns; the
  counts identify the affected rows and the raw tables answer the rest. Revisit if it bites.
- **Conjunction of non-author and non-bot.** `first_non_author_comment_at` can be satisfied by a bot
  reply, so it does not strictly mean "someone responded." The two axes are exposed separately
  rather than crossed; crossing them is a column-budget question, not a technical one.
- **`schema` view/table type column and view-definition printing.** #12's remaining scope.
- **`_ghtriage_meta` visibility in `schema`.** #12's open question; unchanged here.
- **Issue-event columns** (`last_event_at`, `last_label_change_at`). Blocked on the raw-data-breadth
  work; would be additional narrow columns, not folded into anything blended.
- **Nested-type rendering in `table` / `csv` output.** `labels` surfaces Python `repr`. Pre-existing
  formatter behavior, separate decision.

# Decision log

Design decisions that a future reader might reasonably undo without knowing why they were made.

Findings are not decisions. That roughly half of `issue_comments` rows belong to pull requests is a
fact, and it lives in the plan documents and the column comments; that the two comment channels are
kept separate rather than summed is a decision, and it lives here.

Entries link the plan document that carries the full reasoning, and cite the issue number. Link the
plan at its archived path: `docs/plans/archive/` is where implemented plans come to rest, so the
path is stable once a plan lands. Only link a plan still in `docs/plans/` by filename, since it has
a move ahead of it.

Append new entries at the end.

---

## Derived activity views

From [#11](https://github.com/jayqi/ghtriage/issues/11) — see
[the plan](plans/archive/2026-08-06-derived-activity-views.md) for the full reasoning and the data
behind it.

**Pull request conversation and review comments stay separate.**
Rejected: a single combined comment count. GitHub's `/issues/comments` endpoint returns PR
conversation comments and `/pulls/comments` returns only inline review comments. In the sample
repository, 61% of pull requests had conversation comments and no review comments at all, so a
combined or review-only count reports them as silent.

**Split counts carry the total under the unqualified name, with a `non_bot_` companion.**
Rejected: making `comment_count` mean the non-bot count. `issues.comments` is GitHub's own total
sitting in the raw table under a near-identical name, and excluding bots from the unqualified name
is itself the judgment the project avoids. The companion counts non-bots rather than bots because
`WHERE non_bot_comment_count = 0` is the query people actually want and needs no arithmetic.

**No AI-identification column; `author_type` is as far as the data supports.**
Rejected: a curated list of agent logins. GitHub distinguishes `Bot` from `User`, not AI from CI —
`github-actions[bot]` and `Copilot` are both `Bot`, and machine accounts that are not GitHub Apps
are typed `User`. Separating them needs a maintained, repo-specific list, which goes stale and is a
judgment. If it is ever wanted, `.ghtriage/config.toml` is the right home, not the database.

**The non-bot filter is `IS DISTINCT FROM 'Bot'`, not `<> 'Bot'`.**
Rejected: the plain comparison. A NULL `user__type` from a deleted account makes `<>` evaluate to
NULL, dropping the row from both buckets and breaking `bot = total - non_bot`.

**`pending_reviewers` rather than GitHub's `requested_reviewers`.**
Rejected: preserving the upstream field name. GitHub drops a reviewer from the list once they submit
a review, so the field holds only unfulfilled requests; the upstream name invites reading it as
review history.

**`labels` / `assignees` / `pending_reviewers` are `VARCHAR[]`.**
Rejected: a delimited string, which would render uniformly across all output formats. The list form
is what makes `list_contains(labels, 'bug')` work; substring matching on a joined string
false-positives on labels that are prefixes of others. Nested values do surface as Python `repr` in
`--format table` and `csv`.

**Assignees come from the child table, never `assignee__login`.**
Rejected: the scalar column. GitHub deprecated it in favour of the array and it does not populate
reliably with more than one assignee — a sampled pull request had two assignees and a NULL
`assignee__login`, so the obvious query returns wrong answers rather than merely inconvenient ones.

**Views are created on pull, not on first query.**
Rejected: creating them lazily at query time, which `execute_query`'s read-only connection
([#8](https://github.com/jayqi/ghtriage/issues/8)) forbids without giving up read-only.

**Views, not materialized tables.**
Rejected: `CREATE TABLE AS`, which would need invalidating whenever a pull changed the underlying
data. Scanning both views fully took 38 ms on the sample repository.

**Views degrade to typed NULLs and empty relations when *optional* upstream data is absent.**
Rejected: skipping view creation whenever anything is missing. dlt only creates a column or child
table once a record carrying that field arrives, so a young or sparse repository is missing several.
Degrading keeps the column set identical everywhere, so a query written against one database works
against another. Only a missing *base* table skips a view.

**Views are plain SQL templates with format slots; column docs live in a separate dict.**
Rejected: a record-per-column composition layer colocating SQL and docs. Colocation does not
actually enforce anything — an edited expression can leave an adjacent doc stale just as easily.
`test_view_docs_match_view_columns` gives the stronger guarantee, catching drift in both directions,
and the SQL stays readable and pasteable into `ghtriage query`.

**Padding types must match what dlt produces exactly.**
Rejected: trusting the `UNION ALL BY NAME` padding to be inert. It coerces rather than erroring, and
it runs on every pull rather than only when a column is absent, so a mistyped entry silently
rewrites real values — a naive `TIMESTAMP` padded as `TIMESTAMP WITH TIME ZONE` is reinterpreted in
the machine's local zone. `test_padding_does_not_coerce_existing_column_values` pins the
values, and `test_view_types_match_spec_on_sparse_databases` pins the types on the sparse
databases where the padding and the empty stand-ins are load-bearing.

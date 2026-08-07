# Decision log

Non-obvious choices in this project, each recorded with the alternative that was rejected. Read
before changing behavior an entry covers: several choices look arbitrary without the reasoning, and
undoing them reintroduces problems that are already known.

## How to use this file

**What belongs here.** Two tests, and an entry has to pass both.

1. A future reader might reasonably undo it without knowing why it was made.
2. There is no single code site where the reasoning would fit.

The second test is what keeps this file from becoming a line-by-line annotation of the source. If a
comment beside the line would do the job, write the comment instead — it reaches the person about to
change that line, who would otherwise have to think to look here. What passes both tests is what the
code exposes to callers (shape, names, types) and how a module is put together: reasoning that lives
across the SQL, the docs dicts, the tests and the README at once.

Findings are not decisions — that roughly half of `issue_comments` rows belong to pull requests is a
fact, and it belongs in a plan document and in the column comments; that the two comment channels
are kept separate rather than summed is a decision, and it belongs here.

**Where it goes.** Group entries under a dated heading naming the work, newest section last. Use the
date of the work's plan document so the two line up:

```
## YYYY-MM-DD — Short title of the work

From [#N](https://github.com/jayqi/ghtriage/issues/N) — see
[the plan](plans/archive/YYYY-MM-DD-name.md) for the full reasoning and the evidence behind it.
```

Link a plan at its archived path once it has landed in `docs/plans/archive/`, which is where
implemented plans come to rest, so the path is stable. A plan still sitting in `docs/plans/` has a
move ahead of it — cite that one by filename instead.

**Entry shape.** One bold sentence stating the decision, then a sentence or two on the alternative
and why it lost. Link out rather than restating the plan — the plan holds the full argument, this
holds enough to stop someone reversing it by accident:

```
**The thing that was decided.**
Rejected: the alternative. Why it lost, ideally with the evidence that settled it.
```

**Reversing a decision.** Never rewrite or delete a past entry to erase a reversal — its reasoning
is the record of why the code once looked the way it did. Add a new entry under the current section,
and append one line to the entry it replaces:

```
**Superseded** by the YYYY-MM-DD entry "New decision".
```

Removing an entry that fails test 2 is different, and fine: the reasoning is not being erased, it is
moving to the code. Relocate it to a comment in the same change.

---

## 2026-08-06 — Derived activity views

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

**`pending_reviewers` rather than GitHub's `requested_reviewers`.**
Rejected: preserving the upstream field name. GitHub drops a reviewer from the list once they submit
a review, so the field holds only unfulfilled requests; the upstream name invites reading it as
review history.

**`labels` / `assignees` / `pending_reviewers` are `VARCHAR[]`.**
Rejected: a delimited string, which would render uniformly across all output formats. The list form
is what makes `list_contains(labels, 'bug')` work; substring matching on a joined string
false-positives on labels that are prefixes of others. Nested values do surface as Python `repr` in
`--format table` and `csv`.

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

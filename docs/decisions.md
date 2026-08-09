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

Findings are not decisions — that roughly half of `conversation_comments` rows belong to pull requests is a
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
[the plan](/docs/plans/archive/2026-08-06-derived-activity-views.md) for the full reasoning and the data
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
`test_view_docs_match_view_columns` pins the weaker but enforceable half — every column has exactly
one doc and no doc outlives its column — and the SQL stays readable and pasteable into
`ghtriage query`. What neither colocation nor that test can catch is a doc whose *text* drifts from
its expression; that needs review, and it has already happened once.

**Tables are named for the domain, not for GitHub's REST paths.**
Rejected: mirroring the endpoint names, which gave `pulls`, `issue_comments` and `pull_comments`.
`pulls` collided with the `ghtriage pull` command while having the weakest claim to the word — the
REST path is the only place GitHub spells it that way, against `pull request` in the docs, `gh pr`
in the CLI and `pullRequest` in GraphQL. `issue_comments` was worse than a collision: it holds
conversation comments on issues *and* pull requests, so the name actively misleads. The dlt resource
`name` and `endpoint.path` are separate, so the tables are renamed without touching the API calls.
Spelled out rather than abbreviated (`pull_requests`, not `prs`) because nothing else in the schema
is abbreviated.

## 2026-08-07 — Full-text search

From [#13](https://github.com/jayqi/ghtriage/issues/13) — see
[the plan](/docs/plans/archive/2026-08-07-full-text-search.md) for the full reasoning and the
measurements behind it.

**GitHub's `id` is the full-text document key, not dlt's `_dlt_id`.**
Rejected: `_dlt_id`, which #13 proposed as the natural key. dlt assigns it at extract time rather
than deriving it from content, so an edited row's `_dlt_id` changes on the pull that picks the edit
up — no good as a handle that outlives a pull. `id` is GitHub's permanent identifier, is dlt's merge
key, and is a column agents already select. Uniqueness is the load-bearing part either way:
`create_fts_index` validates nothing, and rows sharing a key all silently report the first one's
score. dlt's merge on `id` is what guarantees uniqueness, so a test pins that declaration on the
source config rather than a runtime check re-verifying the loader on every pull (see the
2026-08-08 section "Defensive-layer simplification").

**Thread tables are materialized tables, not views.**
Rejected: views, per the 2026-08-06 entry "Views, not materialized tables". That entry rejected
materialization because of the invalidation cost; here it is already paid. DuckDB cannot index a
view at all, and an FTS index has no incremental update — it is rebuilt from scratch on every pull
regardless — so the table is rebuilt in the same step for 5–13 ms.

**Thread documents fold both pull request comment channels together.**
Rejected: mirroring the separation `pull_request_activity` keeps for counts. Whether a PR got
discussion or code review is a fact about engagement and stays split; a search corpus just wants all
the words.

**The derived views carry `id` as their first column.**
Rejected: leaving it out as dlt plumbing redundant with `number`. `match_bm25` is keyed on the
document id and is not bound to the indexed table, so this one column turns "search, then filter on
derived facts" from a join into a plain `WHERE`. It leads the projection rather than trailing it
because a key nobody can find is a key nobody uses. It is padded like any other optional column, so
a database whose base table predates it still gets the full column set.

**DuckDB's default tokenizer is kept, so digits are not searchable.**
Rejected: `ignore='(\.|[^a-z0-9])+'`, which makes `404` findable but stops `python` matching
`python3.11` — it tokenizes as `python3` + `11`. Neither setting is free. Exact codes and versions
are what `LIKE` and `regexp_matches` already do well, and full-text search is a complement to the
SQL filters rather than a replacement.

**No `search` subcommand and no SQL macro wrapper.**
Rejected: a `search_issues(q)` table macro. It works and composes with `WHERE`, but macros are
invisible to `information_schema.tables` and reject `COMMENT ON`, so the schema-transparency
mechanism the rest of the project relies on cannot document them. The raw syntax is surfaced through
`ghtriage schema` instead.

**An explicit `duckdb>=1.2` pin.**
Rejected: continuing to inherit duckdb through `dlt[duckdb]`, whose floor is `duckdb>=0.9` — a
floor under which `COMMENT ON` does not exist, so the project has been misdeclaring its
requirements since #11. 1.2 is where the full-text behavior here was checked by hand. Note that CI
has no duckdb matrix: it resolves the lockfile, so only the pinned version is ever exercised and
the floor is asserted rather than tested. Worth a floor job if the range ever widens.

## 2026-08-08 — Derived objects in one module

Follow-up reorganization after [#13](https://github.com/jayqi/ghtriage/issues/13); no behavior
changed.

**Views and thread tables live together in `derived.py`, built by one `create_derived`.**
Rejected: keeping the thread tables with the indexer that consumes them, on the grounds that
building and indexing in one function cannot be mis-ordered. But `_create_one` and
`_build_thread_table` were the same function twice — same skip-if-base-missing, same
`CREATE OR REPLACE` through the slot renderer, same comment reapplication, same catch-and-warn —
and sharing helpers between two modules would have left both copies of the loop. The four objects
are one kind of thing to a user and now to the code: one registry, one build path parameterized by
`VIEW` or `TABLE`, one docs convention, one drift guard covering all four.

**`full_text_search` depends on `derived` having run, and that is not treated as a hazard.**
Rejected: defending the ordering with structure. Materialize, then index is how indexing works
everywhere; a maintainer arrives knowing it. `run_pull` pins the order, a test pins `run_pull`,
and `create_search_indexes` states the precondition. What makes a failure survivable rather than
silent is that neither module leaves behind an object it could not rebuild this pull: a derived
object or index that cannot be built is dropped, so a later search errors on a missing relation
instead of scoring the previous pull's text. See the 2026-08-07 entry "Thread tables are
materialized, not views" for why they are tables at all.

**Every step after the load is attempted, reported, and survived, and that decision lives in
`run_pull`.**
Rejected: each builder guarding itself. A module swallowing its own failures is deciding what a
pull is worth, which is the orchestrator's call. The builders raise, and return a message for any
single object they could not build; `run_pull` collects both and exits 0, because the raw tables
landed and they are what a pull is for. The CLI follows any warnings with a pointer at
`ghtriage pull --full`, which is the recovery path for every post-load state. Only the skips stay
inside the builders, as notes on stderr: an object with no source table yet is not a failure, and a
young repository legitimately skips several.

## 2026-08-08 — Defensive-layer simplification

Scope reduction of the #13 defensive machinery before it shipped — see
[the plan](/docs/plans/2026-08-08-simplify-derived-and-fts.md) for the failure taxonomy and boundary
rule these decisions apply, and for the review protocol that goes with them.

**Absent upstream data is handled by one declaration-driven padding combinator, applied to every
source a derived object reads — base tables included.**
Rejected: four coexisting mechanisms — padding CTEs inside the SQL, a stand-in dict for absent
tables, per-slot column probes deciding real-vs-stand-in, and `requires` tuples gating whole
objects — each skip path needing its own drop logic, stderr note, and tests. Padding by
`UNION ALL BY NAME` makes a missing column a non-event without probing, so the only remaining
check is whether the base table exists at all. The 2026-08-06 decision that views degrade to
typed NULLs and empty relations stands; this changes the mechanism, not the behavior.

**A `sources` declaration lists every column the SQL reads, not just the ones dlt might omit.**
Rejected: padding only observed-missing columns. Deciding which columns "might" be absent is a
judgment renewed at every review; declaring all of them is mechanical, self-documenting, and
padding an always-present column costs nothing.

**No runtime key-uniqueness check before indexing.**
Rejected: `_key_is_usable`, a per-pull full-table scan re-verifying what dlt's merge on `id`
already guarantees. A broken merge key means the whole database is wrong, not just BM25 scores. A
test pins `primary_key: id` + `write_disposition: merge` on every resource declaration instead —
the layer that provides the guarantee is the layer that gets the test.

**A full-text index covers all its declared columns, or is not built.**
Rejected: indexing whichever declared columns happen to exist. A search that silently covers only
`title` because `body` was absent returns plausible, wrong-by-omission results — the one failure
class this project defends hardest against. A build over a missing column fails into the existing
drop-and-warn path instead. This is also what lets `ghtriage schema` report indexes from the
declaration: a present index always matches it exactly.

**A post-load failure warns and advises `pull --full`; nothing is dropped beyond the object that
failed.**
Rejected: cascading `drop_derived_objects` when the derive step failed wholesale. The per-object
paths already drop what they could not rebuild; the wholesale case leaves the user one known,
cheap command from clean, and the CLI now says so next to the warnings.

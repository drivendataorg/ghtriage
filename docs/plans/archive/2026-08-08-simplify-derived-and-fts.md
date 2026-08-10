# Plan: Simplify the defensive layer around derived objects and full-text search

Follow-up to [#13](https://github.com/jayqi/ghtriage/issues/13), applied before that branch ships.
The full-text design in [the 2026-08-07 plan](/docs/plans/archive/2026-08-07-full-text-search.md)
is sound and unchanged: BM25 through DuckDB's `fts` extension, GitHub's `id` as the document key,
materialized thread tables, everything rebuilt on every pull. What changes is the defensive
machinery that accreted around it across review rounds — and, so it does not accrete again, this
plan also states how defensive code is budgeted in this project and how review findings are
triaged before they become fixes.

## Context

The #13 implementation went through repeated review cycles, and each cycle followed the same
shape: a reviewer asked "what if X is missing or stale?", the fix added a code path for X, and
that path then needed its own drop behavior, stderr note, tests, and decision-log entry — which
the next review round found new edge cases in. The result is that one underlying fact — *dlt
creates a table or column only once data arrives for it* — is defended by four separate
mechanisms:

1. `UNION ALL BY NAME` padding CTEs inside the SQL templates, for base-table columns;
2. the `EMPTY` dict of stand-in relations, for whole optional tables;
3. per-slot `sources` column sets probed with `present_columns`, deciding real-vs-stand-in;
4. `requires` tuples gating whole objects on base-table columns.

Around these sit roughly eight distinct skip-and-drop branches across `derived.py` and
`full_text_search.py`, each with its own tests — on the order of 25–30 tests exist to pin
degradation behavior rather than answers.

The operating facts that should have set the defense budget:

- The database is a **disposable per-user cache**: `ghtriage pull --full` recreates everything
  from the GitHub API in minutes. No state in it is precious. This is a property of the
  architecture, not of the user count — it stays true for every user if the tool is published.
- The tool is **single-user today** and not yet on PyPI, which makes breaking changes free right
  now. That fact is about today only; the taxonomy below deliberately rests on the cache being
  disposable rather than on the user base staying small.
- The consumers are agents running SQL. The failure mode that actually hurts them is a
  **plausible wrong answer**; a loud error is something they recover from in one step.

Under those facts, most of the defended states are either impossible (the loader guarantees them
away), self-healing (the next pull rebuilds everything), or fixable by one command the user
already knows.

## How to think about defensive code in this project

Two rules. A condensed version lives in `AGENTS.md` so it is in context for every future session;
this section is the full argument.

**Rule 1 — classify the failure before writing the defense.** Four classes, in descending order
of how much defense they deserve:

1. **Silent wrong answers.** Aggregates that count the wrong rows, join keys that mismatch,
   classifications that flip on NULL. Defend hard: careful logic, and a test per hazard. This is
   the class the project exists to get right, and nothing in this plan weakens it.
2. **Loud errors.** A query or pull that fails with a message naming the problem. This is an
   acceptable outcome, not a bug to code around. Code that converts a would-be loud error into a
   handled path must justify itself against class 1: a handled path that changes what a query
   silently returns is usually worse than the error.
3. **States one `ghtriage pull --full` fixes.** Stale derived objects after a mid-pull failure,
   a half-built database. A warning that names the state and points at `--full` is the whole
   defense. The recovery command is cheap and total, and because the warning itself names it,
   this class holds just as well for a user who did not write the tool.
4. **States an upstream layer guarantees impossible.** Duplicate or NULL `id` values (dlt merges
   on `id` as the declared primary key), a table that exists but has no `_dlt_id`. No runtime
   defense at all — pin the upstream guarantee with a test on the declaration, and consume it
   downstream as fact.

**Rule 2 — establish invariants at boundaries; consume them downstream.** When something might
be absent or malformed, the first question is whether one step at a boundary can make it
guaranteed-present, not how each use site should cope. One normalization point replaces N
defensive sites, and the paths that no longer exist need no drop logic, no stderr notes, and no
tests. Code paths that don't exist have no edge cases.

What these rules are **not**: license to skip NULL handling inside the SQL (that is class 1 —
`IS DISTINCT FROM`, `TRY_CAST`, `COUNT(column)` vs `COUNT(*)` all stay), and not a ban on
graceful degradation where it is one mechanism doing the work (the padding combinator below *is*
graceful degradation — once, at the boundary).

## What stays

For the avoidance of doubt, these current behaviors are correct under the rules above and are
kept:

- Every aggregate-correctness defense and its tests: the non-bot count semantics, non-author
  filters, participant-set construction, list columns not fanning out, the URL-regex join key
  with `TRY_CAST`.
- Rebuild-everything-on-pull, and the `run_pull` ordering (load → derive → index → annotate)
  pinned by a test.
- Per-object build isolation: an object whose `CREATE` fails is dropped and reported as a
  warning, and the rest still build.
- The quiet skip for a base table that does not exist yet (a young repository with no PRs is
  normal, not a failure), which also drops any previously built object so nothing stale
  survives.
- `run_pull`'s step isolation: the raw load is what a pull is for; every later step is
  attempted, reported, and survived.
- Schema transparency for agents: `COMMENT ON` docs, the drift-guard tests between SQL and doc
  dicts, and the `ghtriage schema` index block.

## Specifications

### 1. One padding combinator replaces four mechanisms

All source normalization moves into a single declaration-driven renderer. Every table an
object's SQL reads — the base table included — becomes a `{slot}` in the template, and each
object declares, per slot, the columns its SQL reads with their DuckDB types:

```python
@dataclass(frozen=True)
class Derived:
    kind: str                           # VIEW or TABLE
    base: str                           # slot that must exist for the object to be built
    sql: str                            # template with a {slot} per source table
    sources: dict[str, dict[str, str]]  # slot -> {column: DuckDB type} the SQL reads


def render_source(table: str, columns: dict[str, str], present: set[str]) -> str:
    typed_nulls = ", ".join(f"NULL::{ctype} AS {name}" for name, ctype in columns.items())
    if table not in present:
        return f"(SELECT {typed_nulls} WHERE false)"
    return f"(SELECT * FROM github.{table} UNION ALL BY NAME SELECT {typed_nulls} WHERE false)"
```

An absent table renders as an empty relation with exactly the declared columns; a present table
renders padded, so any declared column dlt has not created yet exists as a typed NULL. Either
way the SQL downstream sees every column it reads, so **no column probing is needed at all**.

Declare **every** column the SQL reads from a slot, not just the ones observed missing on sparse
repositories. The judgment call "which columns might dlt omit?" is exactly the kind of reasoning
that fed the spiral; declaring all of them makes each `sources` entry a complete, mechanical
contract, and padding a column that is always present is free.

Consequences, all deletions:

- The `EMPTY` dict, `render_slots`, `present_columns`, the `requires` field, the per-slot
  real-vs-stand-in `usable` set logic, and the column-availability skip branches in
  `_create_one` (missing `id`, missing required columns) — the only remaining skip is the
  base-table presence check, which keeps its quiet stderr note and its drop.
- The `issues_padded` / `pulls_padded` CTE bodies in the four SQL templates reduce to
  `SELECT * FROM {issues}` (respectively `{pull_requests}`); the typed-NULL lists move into the
  `sources` declarations. Shared column dicts (e.g. one for conversation-comment facts, one for
  `_dlt_parent_id`+`login` child tables) replace the `_CONVERSATION` / `_LOGINS` tuples.
- The existing non-coercion caveat still applies and keeps its tests: a declared type that
  mismatches what dlt produces coerces silently under `UNION ALL BY NAME`, so
  `test_padding_does_not_coerce_existing_column_values` and
  `test_view_types_match_spec_on_sparse_databases` are retargeted at the combinator, not
  deleted.
- A new drift guard replaces `test_every_format_slot_has_an_empty_relation`: every `{slot}`
  referenced in an object's SQL template must appear in its `sources` (parseable with
  `string.Formatter`), and vice versa.

Where the declared types come from: they must match what dlt actually produces, because a
mismatch coerces silently under `UNION ALL BY NAME` rather than erroring. The current padding
CTEs and `EMPTY` dict hold already-vetted types for every column padded today; for columns newly
declared under the every-column rule, take types from a real pulled database (`ghtriage schema
<table>`) or the typed test fixtures — do not guess. Two easy-to-miss declarations: `_dlt_id` on
the base tables and `_dlt_parent_id` on the child tables are read by the label/assignee/reviewer
joins, so they are declared like any other column (both `VARCHAR`). The retained
non-coercion tests are the backstop if a type is still wrong.

Note the interaction with #13's decision that the views carry `id`: `id` is declared like any
other column, so a sparse database still gets the full column set, unchanged.

### 2. The key-uniqueness runtime check becomes a declaration test

Delete `_key_is_usable` and its skip path. Duplicate or NULL `id` values are a class-4 state:
dlt merges on `id` as the declared `primary_key` of all four resources, so the guarantee lives
in the loader, and re-verifying it with a full-table scan on every pull is defending against the
loader being broken — a state in which the whole database is wrong, not just BM25 scores.

The original plan's finding 3 said uniqueness was "worth a test rather than an assumption"; the
implementation escalated that to runtime machinery. Honor the original intent: a test asserts
that every resource in `build_rest_api_source`'s config declares `primary_key: "id"` and
`write_disposition: "merge"` — pinning the invariant the index relies on at the layer that
provides it.

### 3. An index covers all its declared columns, or is not built

Delete the `indexed = [column for column in columns if column in available]` subset logic and
the key-column / no-text-columns skip branches in `_index_one`. Indexing a silently reduced
column set is a class-1 hazard dressed up as robustness: a search over `issues` that quietly
covers only `title` because `body` was absent returns plausible, wrong-by-omission results.

New behavior: attempt `create_fts_index` over exactly the declared columns. If the table is
absent, skip quietly and drop any previous index (unchanged). If the `PRAGMA` fails for any
reason — a declared column genuinely missing, or anything else — the existing catch-all already
does the right thing: drop the index and return a warning. One failure path instead of five
skip paths. `full_text_search.py` should land at roughly 50 lines.

This also makes the declaration authoritative: a present index always has exactly the declared
key and columns, which is what spec 5 relies on.

### 4. `run_pull` failure handling: warn and advise, no cascade

Delete the nested `drop_derived_objects` cascade (and the function — its only caller is the
cascade) from `run_pull`. Wholesale `create_derived` failure is a class-3 state: the per-object
paths already drop what they individually could not build, and the wholesale case (database
cannot be opened or probed) leaves the user one command from clean.

Instead, make the warning actionable at the CLI: when `_run_pull` printed any warnings, follow
them with one stderr line pointing at the recovery command, e.g.:

```
A full refresh rebuilds everything the failed steps produce: ghtriage pull --full
```

The per-step `try`/`except` blocks in `run_pull` and the collected-warnings return stay as they
are — that structure is one mechanism, correctly placed.

### 5. Index reporting reads the declaration, not the FTS catalog

Rewrite `get_full_text_indexes` in `query.py`: iterate `INDEXES` in declaration order, keep the
tables whose `fts_github_<table>` schema exists, and take the document count from
`SELECT count(*) FROM github.<table>` — equal to the index's `docs` count because table and
index are rebuilt in the same pull and nothing writes between pulls. Delete the
`fields`/`docs` catalog introspection, the per-index exception swallowing and its stderr
warning, and the `key_column: str | None` nullability (`key_column` becomes `str`, always from
the declaration; an `fts_github_%` schema ghtriage did not declare is ignored rather than
half-reported).

Spec 3 is what makes this sound: an index, when present, matches its declaration exactly.

Knock-on simplifications in `cli.py`: the results arrive in declaration order, so the
`min(..., key=declaration order)` example-picker becomes `indexes[0]`, and the
`index.key_column or "id"` fallbacks go away.

### 6. Trim the `ghtriage schema` prose

The index block and worked example stay — an index agents cannot discover may as well not
exist. The closing paragraph shrinks from five lines to two, dropping the cross-index
id-collision speculation (a misuse nobody has made, defended in prose):

```
The macro is keyed on the document id and is not bound to the indexed table, so it also works
from any relation carrying that id. An id the index does not hold scores NULL.
```

## Files changed

| File | Change |
|---|---|
| `src/ghtriage/derived.py` | Spec 1: `render_source` combinator, `Derived.sources` as typed dicts, delete `EMPTY`/`render_slots`/`present_columns`/`requires`, slim `_create_one`; delete `drop_derived_objects` (spec 4) |
| `src/ghtriage/full_text_search.py` | Specs 2–3: delete `_key_is_usable` and the column skip branches; `_index_one` becomes attempt-or-drop-and-warn (~50 lines total) |
| `src/ghtriage/pipeline.py` | Spec 4: remove the drop cascade from `run_pull` |
| `src/ghtriage/cli.py` | Spec 4: `--full` advice line after warnings; specs 5–6: `indexes[0]` example picker, drop `or "id"` fallbacks, trimmed prose |
| `src/ghtriage/query.py` | Spec 5: declaration-driven `get_full_text_indexes`, `key_column: str` |
| `tests/` | Per the disposition table below; no new test files |

No new modules, no dependency changes, no CLI surface changes beyond output text.

## Test disposition

Deletions, by the spec that removes the behavior:

| Spec | Tests to delete |
|---|---|
| 1 | `test_derived.py`: `..._skips_thread_table_when_base_lacks_the_document_key`, `..._thread_table_degrades_when_comment_body_column_missing`, `..._thread_table_degrades_when_base_body_column_missing`, `..._view_degrades_when_a_comment_column_is_missing`, `..._survives_a_probe_failure_on_one_object`, `test_every_format_slot_has_an_empty_relation` (replaced by the two-way slot drift guard) |
| 2 | `test_full_text_search.py`: `test_skips_table_when_key_is_not_unique`, `test_skips_table_when_key_is_null`, `test_skipping_a_rebuild_for_an_unusable_key_drops_the_previous_index` |
| 3 | `test_full_text_search.py`: `test_indexes_present_subset_when_a_column_is_missing`, `test_skips_table_when_no_text_column_is_present`, `test_skips_table_when_key_column_is_missing`, `test_skipping_a_vanished_key_column_drops_the_previous_index` |
| 4 | `test_pipeline.py`: `test_run_pull_drops_derived_objects_when_the_derive_step_fails`, `test_run_pull_does_not_drop_derived_objects_when_the_derive_step_succeeds`; `test_derived.py`: `test_drop_derived_objects_removes_views_and_tables`, `test_drop_derived_objects_is_a_no_op_when_they_were_never_built` |
| 5 | `test_query.py`: the catalog-introspection and unreadable-index cases of `get_full_text_indexes` |
| 6 | `test_cli.py`: `test_schema_listing_warns_that_a_wrong_index_returns_no_rows` (retarget at the two-line prose) |

Retargeted, not deleted: the padding/typing tests named in spec 1; the sparse-database
column-set tests (`..._pads_missing_column_with_typed_null`, `..._column_set_identical...`,
`..._substitutes_empty_relation...`, `..._pads_id_when_base_table_lacks_it`); the base-absent
skip-and-drop tests in both modules; `test_swallows_errors_from_one_table` and
`test_a_failed_rebuild_drops_the_previous_index` (now the single failure path of spec 3).

Additions: the resource-declaration test of spec 2; the two-way slot drift guard of spec 1; a
CLI test that a pull with warnings prints the `--full` advice line of spec 4.

Everything pinning search behavior, aggregate answers, ordering, idempotence, comments, and
cross-module drift (`..._thread_tables_carry_what_the_indexer_declares`) is untouched.

## Documentation changes

Applied alongside this plan rather than with the code, since they record decisions rather than
code state:

- **`AGENTS.md`** gains a short "Defensive code and review findings" section — the condensed
  rules 1 and 2 plus the triage protocol below. That file is read every session, which is what
  makes the policy durable; this plan holds the full argument.
- **`docs/decisions.md`**: the #13-era entries this plan reverses are edited in place — the
  branch has not shipped, so the never-rewrite convention (which protects shipped history) does
  not apply. The runtime key check, the four-mechanism degradation story, and the drop-cascade
  reasoning come out; a new dated section records the decisions here, citing this plan.
- **The 2026-08-07 plan** gets a header note (matching its existing `views.py` note) saying the
  defensive machinery it specified was simplified before shipping, pointing here. Its findings —
  the `_dlt_id` churn measurements, the index-schema naming, the rebuild timings — remain the
  evidence this plan builds on and are not restated.

## Review protocol

How to run and process reviews of this change — and future changes — so the fix-review-fix
spiral does not restart:

1. **Findings are triaged before anything is fixed.** Each finding gets classified against the
   four failure classes in this plan (silent wrong answer / loud error / `--full`-recoverable /
   upstream-guaranteed impossible). Only class 1 mandates a code change. Classes 2–4 get, at
   most, a warning message, a test pinning the upstream guarantee, or a wont-fix with the class
   named.
2. **A finding must name a concrete trigger inside this system's actual lifecycle.** "The table
   could vanish between pulls" is not admissible unless the reviewer can say what, in this
   system, vanishes it. A finding must also state the harm *given the warnings that already
   exist* — "the user sees stale data after ignoring a warning that told them to run `--full`"
   is a much smaller finding than "the user sees stale data".
3. **Fixes prefer boundaries.** A fix that adds a new code path must say why the invariant
   cannot instead be established at a boundary (rule 2). "Handled at each use site" is the
   pattern this plan exists to remove.
4. **Scope expansion is a question, not work.** A finding about a scenario outside the plan's
   spec is surfaced to the maintainer as a question; it does not get silently fixed into the
   branch.
5. **The stop signal.** When a review round's findings concern only code added by the previous
   round's fixes, the spiral has started: stop patching, and look for the simplification that
   removes the code the findings are about.
6. **Reviewers get the policy.** Review sub-agents are pointed at the `AGENTS.md` section (or
   have it included in their prompt) so findings arrive pre-classified where possible.

## Verification

- `just test` — full suite green after each spec lands.
- `just lint` — clean.
- Manual smoke against the sample repository: `ghtriage pull --full`, then `ghtriage schema`
  (index block present, trimmed prose), one `match_bm25` query from the printed example, and a
  pull against a young/sparse repository to see the base-absent skips and the warning-advice
  line behave.

## Ordering constraints and the closing sweep

Sequencing and commit granularity are the implementer's choice; red/green per `AGENTS.md`
applies throughout, and the branch ends green. The only real ordering constraint: spec 5
(declaration-driven index reporting) is sound only once spec 3 (all-or-nothing indexing) is in,
because it relies on a present index matching its declaration exactly. Everything else is
independent — spec 1 is merely the largest diff, not a prerequisite.

Close with a sweep: confirm the test-disposition table above matches what actually happened;
update `docs/decisions.md` if any spec shifted during implementation; move this plan to
`docs/plans/archive/` and fix the two links pointing at its pre-archive path (in
`docs/decisions.md` and the 2026-08-07 plan's header note).

## Deferred

- **Six indexes → four.** The `issues` and `pull_requests` title/body indexes are nearly
  subsumed by the thread indexes for "has this come up before?", but they answer
  "title/body only" scoping cheaply and each costs one dict entry under the simplified
  machinery. Not worth deciding under this plan.
- **Declaring column hints to dlt** so the raw schema always exists after a pull — the
  even-further-upstream version of rule 2. It would interact with dlt's schema evolution and
  merge behavior, which padding deliberately does not touch. Revisit only if the combinator
  proves insufficient.
- **What publishing to PyPI changes.** The intent is to publish eventually, and the taxonomy is
  written to survive that: it rests on the database being a disposable per-user cache, which
  stays true at any user count. What publication does add: warnings must stand without this
  repository's context (the `--full` advice line already does), the duckdb floor deserves a CI
  job (flagged in the decision log's pin entry), and a schema-version stamp becomes worth it so
  an upgraded tool can tell an old database to refresh instead of misreading it. None of that
  reintroduces per-use-site defensive paths.

# Plan: Full-text search over titles, bodies, and comments

Implements [#13](https://github.com/jayqi/ghtriage/issues/13). Builds on the derived views from
[#11](https://github.com/jayqi/ghtriage/issues/11) — see
[that plan](/docs/plans/archive/2026-08-06-derived-activity-views.md) for the view machinery this
one reuses.

> Note: `views.py` was merged into `derived.py` shortly after this plan landed, so the module
> names below describe the state at the time. See the 2026-08-08 entry in
> [the decision log](/docs/decisions.md).

## Context

Finding related content is a high-value triage primitive: duplicate issues, prior discussions of the
same bug, PRs that touched the same topic. `LIKE` scans have no stemming and no ranking, so an agent
that can't express the query in SQL falls back to selecting the text and reading it. That is already
cheaper than paging the GitHub API, but it moves the cost to the agent's context — 2539 documents
totalling 1.7 MB of text on the sample repository — and it returns them unordered, so nothing tells
the agent which ten to read first. Ranking is what the database can do that a `SELECT` cannot.

DuckDB's `fts` extension provides BM25 ranking through `PRAGMA create_fts_index`. This plan builds
indexes over the raw text on every pull, adds two materialized thread tables so "have we discussed
this before?" is answerable in one query, and surfaces both through `ghtriage schema`.

The design principle carried over from #11 holds: **the tool returns ranked matches; the agent
judges relevance.** BM25 scores text similarity, which is a measurement, not a judgment about
whether two issues are duplicates.

## Findings from the data

Investigated against a real pull (`drivendataorg/cloudpathlib`: 324 issues, 256 pull requests, 1534
conversation comments, 425 review comments) on DuckDB 1.4.4, the same repository #11 used.

### 1. The index schema is named for the containing schema, not `main`

DuckDB's documentation shows `fts_main_<table>`, which reads as a fixed prefix but is not one — the
middle segment is the schema containing the indexed table. Ours live in `github`, so every index
schema is `fts_github_<table>`:

| Indexed table | Text columns | Index schema |
|---|---|---|
| `issues` | `title`, `body` | `fts_github_issues` |
| `pull_requests` | `title`, `body` | `fts_github_pull_requests` |
| `conversation_comments` | `body` | `fts_github_conversation_comments` |
| `review_comments` | `body` | `fts_github_review_comments` |

`match_bm25` also works directly in `WHERE` and `ORDER BY`; the subquery is only needed to *return*
the score as a column.

### 2. `id`, not `_dlt_id`, is the document key

#13 proposed `_dlt_id` and asked whether it survives incremental merges. Measured answer: dlt
assigns `_dlt_id` at extract time rather than deriving it from row content, so **any row that goes
through the merge again comes out with a new one**. Under a realistic incremental run — one edited
row, one row re-served at the inclusive `since` boundary, one never re-fetched:

| row | second run | `_dlt_id` |
|---|---|---|
| never re-fetched | — | unchanged |
| edited | re-merged | **new value** |
| re-served at cursor boundary | dropped by dlt's incremental dedup | unchanged |

So the churn is narrower than "every pull": it is exactly the set of rows whose text changed. Since
the index is rebuilt from scratch every pull anyway (finding 4), stability is not what settles this.
What settles it is that `id` is GitHub's permanent identifier, is dlt's declared `primary_key` on
all four resources, is a column agents already select and join on, and remains valid as a handle
across pulls — where `_dlt_id` changes precisely when an issue is edited.

### 3. Key uniqueness is a correctness requirement, not a nicety

`create_fts_index` does not validate the key column. With two rows sharing a key, indexing succeeds
and **both rows report the first row's score**; with a NULL key, the row scores NULL forever.
Nothing warns.

`id` is unique and non-null in all four tables (324/256/1534/425 rows, all distinct), guaranteed by
dlt's merge on it. This is worth a test rather than an assumption.

### 4. Rebuild cost is negligible — build eagerly on every pull

| step | time |
|---|---|
| index `issues` (title, body) | 40 ms |
| index `pull_requests` (title, body) | 27 ms |
| index `conversation_comments` (body) | 84 ms |
| index `review_comments` (body) | 16 ms |
| build + index `issue_threads` | 5 ms + 59 ms |
| build + index `pull_request_threads` | 13 ms + 124 ms |
| **total** | **~370 ms** |

Database grows 27.3 MB → 37.0 MB (+36%), of which the thread tables and their indexes are about
4 MB. Searches run in 5–11 ms.

This closes #13's open question about lazy building. It also could not work: `execute_query` uses a
read-only connection ([#8](https://github.com/jayqi/ghtriage/issues/8)), which forbids creating an
index at query time — the same constraint that made views build-on-pull.

### 5. The `fts` extension needs no special handling

It autoloads on the write path, and a **read-only connection can even autoinstall it** — verified by
pointing a read-only connection at a fresh `extension_directory` and running a search, which
succeeded. Installing writes to the extension directory, not the database.

In practice the pull installs it, so searches never touch the network. Index creation still needs a
try/except: `extensions.duckdb.org` can be blocked in environments where `api.github.com` is not.

### 6. Views cannot be indexed, and carry no prose worth indexing

`create_fts_index` on a view fails: `Catalog Error: … is not an table`. That rules out indexing
`issue_activity` directly, and materializing it to work around the restriction is worse than
useless. The views' only prose column is `title` (46 and 39 characters on average); everything else
is an enum, a login, or a `VARCHAR[]`. Dropping `body` from the corpus roughly halves what a search
finds:

| query | `title` only | `title` + `body` |
|---|---|---|
| `cache directory size limit` | 40 issues | 83 |
| `azure default credential` | 20 | 58 |
| `glob performance` | 12 | 28 |

For completeness: DuckDB *will* index a `VARCHAR[]` column by stringifying it, and `match_bm25` on
`labels` returned exactly the same 64 issues as `list_contains(labels, 'bug')`. Same answer by way
of stemming, stopwords, and ranking — strictly the worse tool for set membership.

### 7. `match_bm25` is keyed on the document id, not bound to its table

The index maps id → document, and the generated macro is an ordinary scalar function. It can be
called from **any relation carrying that id**, including a view. So giving the derived views an `id`
pass-through column buys full `title` + `body` search with derived-fact filtering and **no join**:

```sql
SELECT number, title, labels, comment_count, score FROM (
    SELECT *, fts_github_issues.match_bm25(id, 'glob is slow on large directories') AS score
    FROM issue_activity
) WHERE score IS NOT NULL AND non_bot_comment_count = 0 ORDER BY score DESC LIMIT 10
```

Verified to return results identical to the explicit `JOIN issues USING (number)` form, and
marginally faster (20 ms vs 25 ms — noise at this size). Filters applied inside the view compose
correctly, so there is no ordering trap.

The views do not currently select `id`; they begin at `number`. Adding it is one column each.

### 8. Thread documents answer the question the base indexes answer badly

A comment-level index scores each comment independently and returns comment *rows*, which need the
number parsed out of `issue_url` and then disambiguated — `conversation_comments` covers issues and
pull requests both. For `'anonymous credentials for public buckets'` the top eight comment hits
interleave the two: #271 (issue), #514 (PR), #191 (issue), #124 (PR), then four issues.

One document per thread — title + body + every comment, concatenated — returns ranked issues
directly. Recall differs materially in both directions: the thread index put
[#38](https://github.com/drivendataorg/cloudpathlib/issues/38) "Support anonymous through boto to
work with public S3 assets" in its top three, which the comment index misses in its top five
because that issue's relevance is spread thinly across several comments that each score weakly.
Conversely a single sharp comment inside a long thread ranks lower once diluted into a 2280-character
document. They are complementary, which is the argument for keeping both rather than picking one.

| table | docs | avg length |
|---|---|---|
| `issue_threads` | 324 | 2280 chars (vs 831 for `body` alone) |
| `pull_request_threads` | 256 | 3751 chars |

### 9. The default tokenizer drops digits

DuckDB's default `ignore` pattern is `'(\.|[^a-z])+'`, which strips digits from the indexed text
*and* from the query. Searching `404` therefore returns nothing.

Passing `ignore='(\.|[^a-z0-9])+'` finds `404`, but costs prose matching: `python3.11` then
tokenizes as `python3` + `11`, so a search for `python` no longer matches it. Neither setting is
free. This plan keeps DuckDB's default and documents that exact codes and version strings belong in
`LIKE` / `regexp_matches`, which answer them precisely — full-text search complements the SQL
filters rather than replacing them.

Stemming works as expected on prose: `renaming` matches `rename`.

### 10. A wrong index/table pairing fails closed, silently

Scoring `pull_requests.id` against `fts_github_issues` returns zero rows rather than raising: ids
from another table simply do not resolve as documents (zero id overlap between `issues`,
`pull_requests`, and `conversation_comments` in this repository). An agent that reaches for the
wrong index name gets an empty result set with no error, so the `schema` output has to state the
pairing explicitly.

### 11. Nothing in the current dependency pin guarantees any of this

`dlt[duckdb]>=1.0,<2` resolves duckdb through dlt's own constraint, which is `duckdb>=0.9`. The
project already needs ≥0.10.2 for `COMMENT ON`, so the floor has been implicit since #11. FTS
behavior verified identical on 1.2.0 and 1.4.4; 1.0.0 has no cp313 wheels and fails to build from
source, so it is untestable rather than known-good. Pin `duckdb>=1.2` explicitly.

### 12. dlt's merge is unaffected by the index schemas

Ran a merge-disposition load against a database with `fts_github_*` schemas present: the load
succeeded and the index schemas survived untouched. `create_views` and `annotate_database` both
scope their catalog probes to `table_schema = 'github'`, and `get_tables()` does the same, so the
index tables stay invisible to `schema` output without any filtering change.

One wrinkle: `DROP TABLE` leaves the index schema orphaned. Harmless here — only `--full` removes
tables, and it deletes the whole database file — but it is why every index is created with
`overwrite=1`.

## Output specification

### Index inventory

| Indexed table | Document key | Indexed columns |
|---|---|---|
| `issues` | `id` | `title`, `body` |
| `pull_requests` | `id` | `title`, `body` |
| `conversation_comments` | `id` | `body` |
| `review_comments` | `id` | `body` |
| `issue_threads` | `id` | `thread_text` |
| `pull_request_threads` | `id` | `thread_text` |

All created with `overwrite=1` and DuckDB's default tokenizer settings (`stemmer='porter'`,
`stopwords='english'`, default `ignore`), per finding 9.

### `github.issue_threads` — one searchable document per issue

| Column | Type | Description |
|---|---|---|
| `id` | `BIGINT` | Pass-through of `issues.id`. The full-text document key. |
| `number` | `BIGINT` | Pass-through of `issues.number`. Join key to `issues` and `issue_activity`. |
| `thread_text` | `VARCHAR` | The issue's title, body, and every conversation comment on it, oldest first, newline-joined. |

Exactly one row per row in `issues`.

### `github.pull_request_threads` — one searchable document per pull request

Same three columns, sourced from `pull_requests`, with `thread_text` folding in **both** comment
channels — conversation comments and inline review comments — interleaved by `created_at`. The
separation #11 established for *counts* does not apply here: a search corpus wants all the words,
and `pull_request_activity` remains the place where the two channels stay distinguishable.

Exactly one row per row in `pull_requests`.

### New `id` column on both derived views

`issue_activity` and `pull_request_activity` each gain `id` as their **first** column, a
pass-through of the base table's `id`. First position rather than after `number` because it is the
key the search syntax requires, and burying it makes the zero-join form in finding 7 harder to
discover. This takes the views to 20 and 24 columns.

Column doc, both views: `Pass-through of <base>.id. The full-text document key: pass it to
fts_github_<base>.match_bm25 to search without joining. Prefer number as the human-facing
identifier.`

### `ghtriage schema` listing

After the existing table listing, a second block:

```
Full-text search indexes

table                  key  indexed columns  documents
---------------------- ---- ---------------- ---------
conversation_comments  id   body                  1534
issue_threads          id   thread_text            324
issues                 id   title, body            324
pull_request_threads   id   thread_text            256
pull_requests          id   title, body            256
review_comments        id   body                   425

Search a table by scoring its key column against its own index:
  SELECT number, title, score FROM (
      SELECT *, fts_github_issues.match_bm25(id, 'segfault on windows') AS score FROM issues
  ) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10

The macro is keyed on the document id, so it also works from any view carrying that id
(issue_activity, pull_request_activity). An id that is not in the index scores NULL, so
pairing a table with another table's index returns no rows rather than an error.
```

`indexed columns` and `documents` are read from the index's own `fields` and `docs` catalog tables,
so they cannot drift from what was actually built. The key column name is not recoverable from the
catalog — `docs` stores key *values* under an opaque `name` column — so it comes from the module's
spec, pinned by a test that compares the declared key column's values against `docs.name`.

The block is omitted entirely when no indexes exist, matching how a missing view is simply absent.

### `ghtriage schema --table issues`

One line above the column table, for any indexed table:

```
Full-text search: fts_github_issues.match_bm25(id, 'query') over title, body (324 documents)
```

### Example queries this enables

```sql
-- Likely duplicates of a new bug report, ranked
SELECT number, title, score FROM (
    SELECT *, fts_github_issue_threads.match_bm25(id, 'timeout uploading large files to S3') AS score
    FROM issue_threads
) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10;

-- Prior discussion, open issues only, nobody but the author has replied
SELECT number, title, labels, score FROM (
    SELECT *, fts_github_issues.match_bm25(id, 'windows path separator') AS score
    FROM issue_activity
) WHERE score IS NOT NULL AND state = 'open' AND first_non_author_comment_at IS NULL
ORDER BY score DESC;

-- Which PRs touched this topic, merged ones first
SELECT number, title, merged_at, score FROM (
    SELECT *, fts_github_pull_request_threads.match_bm25(id, 'retry backoff') AS score
    FROM pull_request_threads
) t JOIN pull_request_activity USING (number)
WHERE score IS NOT NULL ORDER BY merged_at IS NULL, score DESC;

-- All terms required, not just any (BM25 defaults to OR semantics)
SELECT number, title FROM issues
WHERE fts_github_issues.match_bm25(id, 'azure credential', conjunctive := 1) IS NOT NULL;
```

## Implementation

### 1. New module: `src/ghtriage/full_text_search.py`

Named for what it does, not for DuckDB's extension abbreviation — no other module in the project is
abbreviated, and `decisions.md` already rejected abbreviation for table names on the same grounds.
`fts` survives only in identifiers that quote DuckDB's own vocabulary (`PRAGMA create_fts_index`,
the `fts_github_*` schema names agents type), which are not ours to rename. `search.py` is avoided
because #13 non-goals a `ghtriage search` subcommand and the name invites reading it as that
command's home.

Contents:

- `THREAD_TABLES: dict[str, str]` — the two `CREATE OR REPLACE TABLE` templates, written as plain
  SQL with `{slot}` placeholders, exactly as `views.py` writes its views.
- `THREAD_TABLE_DOCS` / `THREAD_COLUMN_DOCS` — mirroring `VIEW_DOCS` / `VIEW_COLUMN_DOCS`, applied
  with `COMMENT ON`, with the same drift-guard test.
- `INDEXES: dict[str, tuple[str, tuple[str, ...]]]` — table → (key column, text columns). The single
  source of truth for both index creation and `schema` output.
- `create_search_indexes(db_path)` — the entry point. Best-effort and guarded at the outermost
  level, like `create_views`: one try/except around the connection and the catalog probe, plus a
  per-object try/except inside, so a locked database or a blocked extension download warns to
  stderr and never fails the pull.

`issue_threads` SQL, validated against the real pull:

```sql
WITH issues_padded AS (
    SELECT * FROM github.issues
    UNION ALL BY NAME
    SELECT NULL::VARCHAR AS title, NULL::VARCHAR AS body WHERE false
),
comments AS (
    SELECT
        TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS number,
        string_agg(body, E'\n' ORDER BY created_at) AS comment_text
    FROM {conversation_comments}
    GROUP BY 1
)
SELECT i.id, i.number, concat_ws(E'\n', i.title, i.body, c.comment_text) AS thread_text
FROM issues_padded i
LEFT JOIN comments c ON c.number = i.number
```

`pull_request_threads` is the same shape with both comment sources `UNION ALL`-ed before the
aggregation, so review and conversation comments interleave by `created_at`.

Three degradation paths, all inherited from #11's approach:

- **Missing base table** — skip that table's thread build and its index, note to stderr.
- **Missing comment table** — the `{slot}` renders to an empty relation and the thread is title +
  body. Requires adding `body` to the `conversation_comments` and `review_comments` entries in
  `views.EMPTY`; the existing stand-ins omit it because the views never select it. Additive and safe
  — the views select named columns, never `*`.
- **Missing text column** — `create_fts_index` raises `CatalogException` on a column dlt never
  materialized (which happens: the pull log warns about exactly this for `milestone`, `auto_merge`,
  and others). Index the intersection of the declared columns and the columns actually present; skip
  the table entirely when that intersection is empty, or when its key column is absent.
- **Comment table present but text-less** — a `conversation_comments` without `body` is not the same
  as an absent one, and only the empty stand-in handles both. `THREAD_SOURCE_COLUMNS` declares what
  a thread source must carry, and a table missing any of it falls back to the stand-in.
- **Unusable key** — a duplicate or NULL document key is checked for and skipped with a warning,
  because `create_fts_index` accepts both and corrupts scores silently (finding 3).

`_present_tables`, `_render` and `_quote` move from private to public in `views.py`
(`present_tables`, `render_slots`, `quote_literal`) and are imported here rather than duplicated.
`EMPTY` is imported as-is.

### 2. `src/ghtriage/views.py`

Add `i.id` / `p.id` as the first projected column of both views, with the doc entries from the
output specification, and pad `id` in `issues_padded` / `pulls_padded` like any other optional
column — a base table without it still gets the full column set rather than losing the view. `test_view_docs_match_view_columns` fails until the docs are added, which is
the intended forcing function.

Promote `_present_tables` → `present_tables` and `_render` → `render_slots`; extend the two comment
entries in `EMPTY` with `NULL::VARCHAR AS body`.

### 3. `src/ghtriage/pipeline.py`

`run_pull` calls `create_search_indexes(db_path)` between `create_views(db_path)` and
`fetch_and_annotate(db_path)` — after the views so the derived-object builders sit together, before
annotation because that step does network I/O and is the likeliest to be slow or fail.

### 4. `src/ghtriage/query.py`

```python
@dataclass
class FullTextIndex:
    table: str
    key_column: str | None   # None for an index ghtriage did not declare
    columns: list[str]
    document_count: int
```

`get_full_text_indexes(cwd=None) -> list[FullTextIndex]` — read `duckdb_schemas()` for
`fts_github_%`, then each index's `fields` and `docs` tables. Returns `[]` when the extension is
absent or no index exists; never raises for a missing index.

### 5. `src/ghtriage/cli.py`

`_run_schema` prints the index block after the table listing, and the one-line index note in
`--table` mode. Both use the existing `_format_table` helper.

### 6. `pyproject.toml`

Add `duckdb>=1.2` to `dependencies` (finding 11).

### 7. `README.md`

New "Full-text search" subsection after "Derived views": what is indexed, the query form, the two
thread tables and why they are separate from the raw tables, the digit limitation from finding 9
with the `LIKE` pointer, and the fail-closed caveat from finding 10. Add `issue_threads` and
`pull_request_threads` to the "What gets pulled" table, marked as derived. Add one search example to
the Examples block.

### 8. `docs/decisions.md`

New dated section, six entries:

- **`id`, not `_dlt_id`, is the full-text document key.** Rejected: `_dlt_id`, as #13 proposed. dlt
  assigns it at extract time, so an edited row's id changes on the pull that picks up the edit;
  `id` is GitHub's permanent identifier, is dlt's merge key, and is a column agents already use.
  Key uniqueness is load-bearing — `create_fts_index` silently gives duplicate keys the same score.
- **Thread tables are materialized, not views.** Supersedes nothing: the 2026-08-06 entry "Views,
  not materialized tables" rejected materialization because of invalidation cost, and that cost is
  already paid here — DuckDB cannot index a view at all, and the index must be rebuilt from scratch
  on every pull regardless, so the table is rebuilt in the same step for 5–13 ms.
- **The derived views carry `id` as their first column.** Rejected: leaving it out as dlt plumbing
  redundant with `number`. `match_bm25` is keyed on the document id and works from any relation
  carrying it, so this column is what makes searching with derived-fact filters a zero-join query.
- **Thread documents fold both PR comment channels together.** Rejected: mirroring the separation
  `pull_request_activity` keeps for counts. A search corpus wants all the words; the counts stay
  split where the distinction is a fact rather than a tokenizer input.
- **DuckDB's default tokenizer is kept, so digits are not searchable.** Rejected:
  `ignore='(\.|[^a-z0-9])+'`, which finds `404` but breaks `python` matching `python3.11`. Exact
  codes and versions are what `LIKE` and `regexp_matches` are good at.
- **No `search` subcommand and no SQL macro wrapper.** Rejected: a `search_issues(q)` table macro.
  It works and composes, but macros are invisible to `information_schema.tables` and reject
  `COMMENT ON`, so the project's entire schema-transparency mechanism cannot document them.

## Tests

**New `tests/test_full_text_search.py`.** Fixture: a temp DuckDB with `github.issues`,
`github.pull_requests`, both comment tables and the child tables, with bodies and comments written
so BM25 ordering is predictable — one issue whose title carries the term, one whose body does, one
where only a comment does, and one with no comments at all.

- `test_creates_index_for_every_declared_table`
- `test_declared_key_column_matches_index_documents` — the declared key's values equal
  `fts_github_<table>.docs.name`, pinning the spec against the catalog (output spec)
- `test_skips_table_when_key_is_not_unique` / `test_skips_table_when_key_is_null` — finding 3's
  silent-corruption guard. The key is validated before indexing rather than assumed: a duplicate
  or NULL key becomes a visible skip instead of a wrong score
- `test_indexed_fields_match_declared_columns` — via each index's `fields` table
- `test_search_ranks_repeated_term_above_single_mention` — BM25 does not weight fields, so this
  asserts term frequency, not title-beats-body, which DuckDB's implementation does not provide
- `test_thread_table_has_one_row_per_base_row` — both thread tables
- `test_thread_text_includes_title_body_and_every_comment`
- `test_thread_text_orders_comments_oldest_first`
- `test_pull_request_thread_includes_both_comment_channels` — finding 8's regression guard
- `test_thread_text_is_title_and_body_when_issue_has_no_comments`
- `test_search_from_a_view_carrying_the_id_matches_search_from_the_base_table` — finding 7
- `test_wrong_index_for_table_returns_no_rows` — finding 10, pinned so the documented caveat stays
  true
- `test_skips_table_when_base_table_missing`
- `test_skips_column_when_column_missing` — index built over the present subset
- `test_skips_table_when_no_text_columns_present`
- `test_thread_table_degrades_when_comment_table_missing`
- `test_is_idempotent` — run twice, identical row counts, columns, comments, and document counts
- `test_reapplies_comments_after_replace` — `CREATE OR REPLACE TABLE` drops comments the same way
  `CREATE OR REPLACE VIEW` does (#11 finding 3)
- `test_swallows_errors` — a broken declaration warns to stderr without raising
- `test_thread_docs_match_thread_columns` — the drift guard, mirroring
  `test_view_docs_match_view_columns`

**Modify `tests/test_views.py`:** `id` present and first in both views; its doc entry present (the
existing drift guard covers the rest); `EMPTY` stand-ins still render with the added `body` column.

**Modify `tests/test_pipeline.py`:** mock `ghtriage.pipeline.create_search_indexes`; assert it is
called once, after `create_views` and before `fetch_and_annotate`, for both `full=True` and
`full=False`.

**Modify `tests/test_query.py`:** `get_full_text_indexes()` returns the declared tables with their
columns and document counts; returns `[]` on a database with no indexes rather than raising.

**Modify `tests/test_cli.py`:** `schema` prints the index block and omits it entirely when no index
exists; `schema --table issues` prints the index line.

## Files changed

| File | Change |
|---|---|
| `src/ghtriage/full_text_search.py` | New — index spec, thread tables, docs, `create_search_indexes` |
| `src/ghtriage/views.py` | `id` column on both views; `present_tables` / `render_slots` made public; `body` added to two `EMPTY` entries |
| `src/ghtriage/pipeline.py` | Call `create_search_indexes` between `create_views` and `fetch_and_annotate` |
| `src/ghtriage/query.py` | `FullTextIndex` dataclass and `get_full_text_indexes()` |
| `src/ghtriage/cli.py` | Index block in `schema`; index line in `schema --table` |
| `pyproject.toml` | `duckdb>=1.2` |
| `README.md` | Full-text search subsection; thread tables in the pulled-data table; search example |
| `docs/decisions.md` | New dated section, six entries |
| `tests/test_full_text_search.py` | New |
| `tests/test_views.py` | `id` column coverage |
| `tests/test_pipeline.py` | Mock and assert `create_search_indexes` |
| `tests/test_query.py` | `get_full_text_indexes()` coverage |
| `tests/test_cli.py` | `schema` index-block coverage |

## Verification

```bash
just lint
just test
```

Manual end-to-end against a real repository (requires `GITHUB_TOKEN`):

```bash
ghtriage pull --full
ghtriage schema
ghtriage schema --table issues
ghtriage schema --table issue_threads
ghtriage query "SELECT number, title, round(score,2) AS score FROM (SELECT *, fts_github_issue_threads.match_bm25(id, 'authentication credentials') AS score FROM issue_threads) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10"
ghtriage query "SELECT number, title FROM (SELECT *, fts_github_issues.match_bm25(id, 'cache directory') AS score FROM issue_activity) WHERE score IS NOT NULL AND state = 'open' ORDER BY score DESC"
ghtriage pull    # incremental; confirm indexes and thread tables survive and stay consistent
ghtriage query "SELECT count(*) FROM issue_threads"
```

Also run once against a repository with no pull requests and once against a young repository whose
issues have empty bodies — the degradation paths are unit-tested, but a real sparse repository is
the honest check.

## Implementation sequence

Red/green TDD per `AGENTS.md`: write the failing test first, watch it fail **for the reason you
expect**, then write the minimum code that makes it pass.

Run `just test` after each step and `just lint` before each commit. `just test` is variadic — use
`just test tests/test_full_text_search.py -k <pattern>` for a tight inner loop.

### Step 1 — Index the base tables

**Red.** `test_creates_index_for_every_declared_table`, `test_indexed_fields_match_declared_columns`,
`test_declared_key_column_matches_index_documents`, `test_key_columns_are_unique_and_non_null`,
`test_search_ranks_title_match_above_body_match`.

**Green.** `full_text_search.py` with `INDEXES` covering the four raw tables and a
`create_search_indexes(db_path)` that creates each with `overwrite=1`.

Build the shared fixture here — the four tables plus child tables, with text chosen so ranking is
deterministic. Getting it right once saves rewriting it in every later step.

### Step 2 — Degradation

**Red.** `test_skips_table_when_base_table_missing`, `test_skips_column_when_column_missing`,
`test_skips_table_when_no_text_columns_present`, `test_swallows_errors`.

**Green.** Present-table and present-column probes, the per-object try/except, and the outer guard.

Confirm the missing-column test fails with `CatalogException` before the guard exists — if it passes
unguarded, the fixture is not reaching the case it claims to.

### Step 3 — Thread tables

**Red.** `test_thread_table_has_one_row_per_base_row`,
`test_thread_text_includes_title_body_and_every_comment`,
`test_thread_text_orders_comments_oldest_first`,
`test_thread_text_is_title_and_body_when_issue_has_no_comments`,
`test_pull_request_thread_includes_both_comment_channels`.

**Green.** `THREAD_TABLES` SQL, the `{slot}` rendering imported from `views.py`, and the build step
ahead of index creation.

The both-channels test is finding 8's regression guard: it must fail if `pull_request_threads` is
built from conversation comments alone.

### Step 4 — Thread table documentation and degradation

**Red.** `test_thread_docs_match_thread_columns`, `test_reapplies_comments_after_replace`,
`test_thread_table_degrades_when_comment_table_missing`, `test_is_idempotent`.

**Green.** `THREAD_TABLE_DOCS` / `THREAD_COLUMN_DOCS` and the `COMMENT ON` pass; `body` added to the
two `EMPTY` entries in `views.py`.

Doing docs before the view work means the drift guard is live for everything that follows.

### Step 5 — `id` on the derived views

**Red.** In `tests/test_views.py`: `id` present and first in both views, with a doc entry. In
`tests/test_full_text_search.py`:
`test_search_from_a_view_carrying_the_id_matches_search_from_the_base_table` and
`test_wrong_index_for_table_returns_no_rows`.

**Green.** `i.id` / `p.id` in the two view templates, plus `VIEW_COLUMN_DOCS` entries.

### Step 6 — Pipeline integration

**Red.** In `tests/test_pipeline.py`, mock `create_search_indexes` and assert call order for both
`full` cases.

**Green.** The call in `run_pull`.

### Step 7 — `schema` surfaces the indexes

**Red.** `tests/test_query.py` for `get_full_text_indexes()` including the empty-database case;
`tests/test_cli.py` for the index block, its omission when no index exists, and the `--table` line.

**Green.** `FullTextIndex`, `get_full_text_indexes()`, and the two `cli.py` output paths.

### Step 8 — Documentation and the version floor

No tests. `duckdb>=1.2` in `pyproject.toml`, the README subsection and example, and the
`docs/decisions.md` section. Then the full manual end-to-end from the Verification section,
including the sparse-repository runs.

## Implementation checklist

**Code**

- [x] Step 1 — fixture; `INDEXES`; `create_search_indexes` over the four raw tables
- [x] Step 2 — present-table / present-column probes and the guards
- [x] Step 3 — `THREAD_TABLES` SQL and the build step
- [x] Step 4 — thread table docs, `COMMENT ON` pass, `EMPTY` extension, idempotence
- [x] Step 5 — `id` on both derived views, with docs
- [x] Step 6 — `run_pull` calls `create_search_indexes` in the right position
- [x] Step 7 — `get_full_text_indexes()` and both `schema` output paths

**Specification conformance**

- [x] All six indexes exist after a pull, over exactly the declared columns
- [x] Every declared key column is unique and non-null in its table
- [x] Both thread tables have exactly one row per base-table row
- [x] Both thread tables and every column carry a `COMMENT ON`
- [x] Both views have `id` first; view column counts are 20 and 24

**Degradation**

- [x] Missing base table skips only that table's index and thread build, with a stderr warning
- [x] Missing comment table still produces thread rows, title + body only
- [x] Missing text column indexes the present subset; no text columns skips the table
- [x] A blocked extension download warns and leaves the pull successful

**Documentation**

- [x] `README.md` — search subsection, thread tables listed, example query
- [x] `docs/decisions.md` — six entries under a new dated section
- [x] `pyproject.toml` — `duckdb>=1.2`

**Final**

- [x] `just lint` clean
- [x] `just test` fully green
- [x] Manual end-to-end on a real repository, including a second incremental `pull`
- [x] Manual check on a repository with no pull requests, and one with empty issue bodies
- [x] This plan moved to `docs/plans/archive/`

## Deferred

- **Incremental index maintenance.** `create_fts_index` only rebuilds from scratch. At ~370 ms for
  a 2500-document repository this is far below the cost of the pull itself. Revisit only if a
  repository large enough to make it matter shows up — the measurement, not the instinct, should
  trigger it.
- **Semantic / embedding search.** A different dependency weight class entirely, and a separate
  discussion if ever.
- **A `ghtriage search` subcommand or SQL macro wrappers.** Rejected above on documentability
  grounds. Revisit if the raw `match_bm25` syntax proves error-prone in real agent use — the
  evidence to watch for is agents pairing the wrong index with a table, which finding 10 shows fails
  silently.
- **Indexing `issues.title` separately from `body` for title-weighted ranking.** `match_bm25` takes
  a `fields` argument that restricts scoring to named columns, so a title-only search is already
  possible against the existing index without building a second one. Field *boosting* — titles
  weighted higher within one score — is not something DuckDB's implementation offers.
- **Searching label and assignee text.** `list_contains` answers set membership exactly; finding 6
  measured BM25 returning the identical rows by a worse route.
- **Digit-preserving tokenization as an option.** Finding 9 makes it a trade, not an upgrade. If
  error-code search turns out to matter more than prose recall, the setting belongs in
  `.ghtriage/config.toml` rather than being switched globally.
- **Cross-repository search.** Out of scope everywhere in the project; the database holds one
  repository.

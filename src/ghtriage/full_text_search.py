"""Full-text search indexes over the derived and raw tables, rebuilt on every pull.

DuckDB's `fts` extension builds a BM25 index with `PRAGMA create_fts_index`, exposing
a `match_bm25(key, query)` macro in a generated schema. The generated schema is named
for the schema holding the indexed table, so ours are all `fts_github_<table>` -- not
the `fts_main_<table>` that DuckDB's documentation shows.

This module only indexes. `derived` builds the thread tables two of these indexes read
from, and runs first -- materialize, then index.

An index that cannot be rebuilt is dropped rather than left: stale, it would keep
scoring the previous pull's rows behind a plausible document count; absent, it fails
the next search outright.
"""

from pathlib import Path
import sys

import duckdb

from ghtriage.derived import DERIVED, present_tables

# table -> (document key column, indexed text columns). Authoritative: an index either
# covers exactly this, or is not there. `id` is unique and non-null because dlt merges
# on it (pinned by test_every_indexed_resource_merges_on_id), so nothing re-checks that
# here -- a broken merge key is a wrong database, not a wrong BM25 score.
INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "issues": ("id", ("title", "body")),
    "pull_requests": ("id", ("title", "body")),
    "conversation_comments": ("id", ("body",)),
    "review_comments": ("id", ("body",)),
    "issue_threads": ("id", ("thread_text",)),
    "pull_request_threads": ("id", ("thread_text",)),
}


def index_schema(table: str) -> str:
    """The schema DuckDB generates for a `github`-schema table's index."""
    return f"fts_github_{table}"


def create_search_indexes(db_path: Path) -> list[str]:
    """Create or replace every declared full-text index over the tables as they stand.

    Raises if the database cannot be opened or probed. A table that cannot be indexed has
    its index dropped and is returned as a message, leaving the rest built. A table that
    is not there yet is skipped quietly -- that is not a failure.
    """
    failures = []
    with duckdb.connect(str(db_path)) as con:
        present = present_tables(con)
        for table in INDEXES:
            if (failure := _index_one(con, table, present)) is not None:
                failures.append(failure)
    return failures


def _drop_index(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Remove any index left from an earlier pull."""
    con.execute(f'DROP SCHEMA IF EXISTS "{index_schema(table)}" CASCADE')


def _index_one(con: duckdb.DuckDBPyConnection, table: str, present: set[str]) -> str | None:
    key_column, columns = INDEXES[table]
    try:
        if table not in present:
            built_here = " (built by `derived`, which runs first)" if table in DERIVED else ""
            print(
                f"Note: skipping full-text index for {table}; "
                f"the table is not present yet{built_here}.",
                file=sys.stderr,
            )
            _drop_index(con, table)
            return None
        # Every declared column, or nothing: a column dlt has not created yet makes the
        # PRAGMA raise into the drop-and-warn below. Indexing the subset that happens to
        # exist would answer plausibly and wrongly by omission instead.
        quoted = ", ".join(f"'{column}'" for column in columns)
        # stopwords='none': the default English list silently eats domain vocabulary on
        # software text ('get', 'old' -- and 'use', whose Porter stem 'us' is on the
        # list), turning a search into a confident empty answer and any conjunctive
        # query containing one such word into zero rows. BM25's IDF weighting already
        # down-ranks ubiquitous terms; commonness needs no list.
        con.execute(
            f"PRAGMA create_fts_index('github.{table}', '{key_column}', {quoted}, "
            f"stopwords='none', overwrite=1)"
        )
    except Exception as exc:
        _drop_index(con, table)
        return f"could not build the full-text index for {table}: {exc}"
    return None

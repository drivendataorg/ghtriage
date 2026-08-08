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

from ghtriage.derived import DERIVED, present_columns, present_tables

# table -> (document key column, indexed text columns)
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


def _key_is_usable(con: duckdb.DuckDBPyConnection, table: str, key_column: str) -> bool:
    """Whether `key_column` can serve as a document key.

    `create_fts_index` validates nothing: rows sharing a key all report the first one's
    score, and a NULL key scores NULL forever, both silently.
    """
    # One comparison covers both, because count(DISTINCT ...) skips NULLs: a NULL key
    # falls short of count(*) exactly as a duplicate does.
    total, distinct = con.execute(
        f"SELECT count(*), count(DISTINCT {key_column}) FROM github.{table}"
    ).fetchone()
    return total == distinct


def create_search_indexes(db_path: Path) -> None:
    """Create or replace every declared full-text index over the tables as they stand.

    Raises if the database cannot be opened or the extension cannot be loaded. A table
    that cannot be indexed is warned about and its index dropped, leaving the rest built.
    """
    with duckdb.connect(str(db_path)) as con:
        present = present_tables(con)
        for table in INDEXES:
            _index_one(con, table, present)


def _drop_index(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Remove any index left from an earlier pull."""
    con.execute(f'DROP SCHEMA IF EXISTS "{index_schema(table)}" CASCADE')


def _index_one(con: duckdb.DuckDBPyConnection, table: str, present: set[str]) -> None:
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
            return
        available = present_columns(con, table)
        if key_column not in available:
            print(
                f"Note: skipping full-text index for {table}; "
                f"key column {key_column} is not present.",
                file=sys.stderr,
            )
            _drop_index(con, table)
            return
        # dlt does not materialize a column that never received data, so index what is
        # actually there. The column set is reported by `schema` rather than assumed.
        indexed = [column for column in columns if column in available]
        if not indexed:
            print(
                f"Note: skipping full-text index for {table}; no text columns are present.",
                file=sys.stderr,
            )
            _drop_index(con, table)
            return
        if not _key_is_usable(con, table, key_column):
            print(
                f"Warning: skipping full-text index for {table}; "
                f"{key_column} is not unique and non-null, which would score rows wrongly.",
                file=sys.stderr,
            )
            _drop_index(con, table)
            return
        quoted = ", ".join(f"'{column}'" for column in indexed)
        con.execute(
            f"PRAGMA create_fts_index('github.{table}', '{key_column}', {quoted}, overwrite=1)"
        )
    except Exception as exc:
        print(f"Warning: could not build full-text index for {table}: {exc}", file=sys.stderr)
        _drop_index(con, table)

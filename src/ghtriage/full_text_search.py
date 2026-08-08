"""Full-text search indexes over the derived and raw tables, rebuilt on every pull.

DuckDB's `fts` extension builds a BM25 index with `PRAGMA create_fts_index`, exposing
a `match_bm25(key, query)` macro in a generated schema. The generated schema is named
for the schema holding the indexed table, so ours are all `fts_github_<table>` -- not
the `fts_main_<table>` that DuckDB's documentation shows.

This module only indexes. `derived` builds the thread tables two of these indexes read
from, and has to run first -- materialize, then index.

Everything here is best-effort: a pull that cannot build an index still succeeds, and
the missing index is visible through `ghtriage schema` rather than announced once and
forgotten.
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
    score, and a NULL key scores NULL forever. Both are silent, so they are checked here
    and turned into a visible skip.

    One comparison covers both: `count(DISTINCT ...)` skips NULLs, so a NULL key shows up
    the same way a duplicate does -- as a shortfall against `count(*)`.
    """
    total, distinct = con.execute(
        f"SELECT count(*), count(DISTINCT {key_column}) FROM github.{table}"  # noqa: S608
    ).fetchone()
    return total == distinct


def create_search_indexes(db_path: Path) -> None:
    """Create or replace every declared full-text index.

    Expects `derived.create_derived` to have run: the thread tables it indexes are built
    there, and indexing a table that has not been rebuilt yet would leave the index a
    pull behind.

    Guarded at the outermost level, like `create_derived`: the connection and the catalog
    probe are inside the try too, so a locked database or a blocked extension download
    warns rather than failing the pull.
    """
    try:
        with duckdb.connect(str(db_path)) as con:
            present = present_tables(con)
            for table in INDEXES:
                _index_one(con, table, present)
    except Exception as exc:
        print(f"Warning: full-text index creation failed: {exc}", file=sys.stderr)


def _drop_index(con: duckdb.DuckDBPyConnection, table: str) -> None:
    """Remove any index from an earlier pull, so a skip leaves nothing behind."""
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

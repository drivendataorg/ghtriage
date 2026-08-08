"""Full-text search indexes over the raw text, rebuilt on every pull.

DuckDB's `fts` extension builds a BM25 index with `PRAGMA create_fts_index`, exposing
a `match_bm25(key, query)` macro in a generated schema. The generated schema is named
for the schema holding the indexed table, so ours are all `fts_github_<table>` -- not
the `fts_main_<table>` that DuckDB's documentation shows.

Everything here is best-effort: a pull that cannot build an index still succeeds, and
the missing index is visible through `ghtriage schema` rather than announced once and
forgotten.
"""

from pathlib import Path
import sys

import duckdb

from ghtriage.views import present_tables, quote_literal, render_slots

# Materialized rather than views, which DuckDB cannot index at all. The rebuild cost is
# already being paid: the index itself has no incremental update, so it is rebuilt from
# scratch on every pull and the table is rebuilt in the same step.
ISSUE_THREADS_SQL = r"""
WITH issues_padded AS (
    -- See views.py on padding: the declared types must match what dlt produces.
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
"""

PULL_REQUEST_THREADS_SQL = r"""
WITH pulls_padded AS (
    SELECT * FROM github.pull_requests
    UNION ALL BY NAME
    SELECT NULL::VARCHAR AS title, NULL::VARCHAR AS body WHERE false
),
comments AS (
    -- Both channels, interleaved by time. The split that pull_request_activity keeps for
    -- counts is a fact about engagement; a search corpus just wants all the words.
    SELECT number, string_agg(body, E'\n' ORDER BY created_at) AS comment_text
    FROM (
        SELECT
            TRY_CAST(regexp_extract(issue_url, '/(\d+)$', 1) AS BIGINT) AS number,
            body,
            created_at
        FROM {conversation_comments}
        UNION ALL
        SELECT
            TRY_CAST(regexp_extract(pull_request_url, '/(\d+)$', 1) AS BIGINT) AS number,
            body,
            created_at
        FROM {review_comments}
    )
    GROUP BY number
)
SELECT p.id, p.number, concat_ws(E'\n', p.title, p.body, c.comment_text) AS thread_text
FROM pulls_padded p
LEFT JOIN comments c ON c.number = p.number
"""

# derived table -> (base table, SQL)
THREAD_TABLES: dict[str, tuple[str, str]] = {
    "issue_threads": ("issues", ISSUE_THREADS_SQL),
    "pull_request_threads": ("pull_requests", PULL_REQUEST_THREADS_SQL),
}

# A comment table is only usable as a thread source if it carries all of these. Present
# but text-less is not the same as absent, and only the empty stand-in handles both.
THREAD_SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "conversation_comments": ("issue_url", "body", "created_at"),
    "review_comments": ("pull_request_url", "body", "created_at"),
}

THREAD_TABLE_DOCS: dict[str, str] = {
    "issue_threads": (
        "Derived table: one full-text document per issue, holding its title, body, and every "
        "conversation comment on it. Rebuilt and re-indexed on every pull. Search it to find "
        "whether a topic has been discussed before; join back on number for the facts."
    ),
    "pull_request_threads": (
        "Derived table: one full-text document per pull request, holding its title, body, and "
        "every conversation and review comment on it. Rebuilt and re-indexed on every pull."
    ),
}

THREAD_COLUMN_DOCS: dict[str, dict[str, str]] = {
    "issue_threads": {
        "id": (
            "Pass-through of issues.id. The full-text document key: pass it to "
            "fts_github_issue_threads.match_bm25 to score this document."
        ),
        "number": "Pass-through of issues.number. Join key to issues and issue_activity.",
        "thread_text": (
            "The issue's title, body, and every conversation comment on it, oldest first, "
            "newline-joined. Comment authors and timestamps are not included; join the raw "
            "tables for those."
        ),
    },
    "pull_request_threads": {
        "id": (
            "Pass-through of pull_requests.id. The full-text document key: pass it to "
            "fts_github_pull_request_threads.match_bm25 to score this document."
        ),
        "number": (
            "Pass-through of pull_requests.number. Join key to pull_requests and "
            "pull_request_activity."
        ),
        "thread_text": (
            "The pull request's title, body, and every comment on it -- conversation and inline "
            "review alike, interleaved oldest first and newline-joined. The two channels are "
            "kept separate in pull_request_activity, where the distinction is a fact about "
            "engagement rather than an input to a tokenizer."
        ),
    },
}

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


def _present_columns(con: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'github' AND table_name = ?",
            [table],
        ).fetchall()
    }


def _key_is_usable(con: duckdb.DuckDBPyConnection, table: str, key_column: str) -> bool:
    """Whether `key_column` can serve as a document key.

    `create_fts_index` validates nothing: rows sharing a key all report the first one's
    score, and a NULL key scores NULL forever. Both are silent, so they are checked here
    and turned into a visible skip.
    """
    total, distinct, nulls = con.execute(
        f"SELECT count(*), count(DISTINCT {key_column}), "  # noqa: S608
        f"count(*) FILTER (WHERE {key_column} IS NULL) FROM github.{table}"
    ).fetchone()
    return nulls == 0 and total == distinct


def create_search_indexes(db_path: Path) -> None:
    """Create or replace every declared full-text index.

    Guarded at the outermost level, like `create_views`: the connection and the catalog
    probe are inside the try too, so a locked database or a blocked extension download
    warns rather than failing the pull.
    """
    try:
        with duckdb.connect(str(db_path)) as con:
            present = present_tables(con)
            usable = {
                table
                for table, columns in THREAD_SOURCE_COLUMNS.items()
                if table in present and set(columns) <= _present_columns(con, table)
            }
            for name in THREAD_TABLES:
                _build_thread_table(con, name, present, usable)
            # Re-probed: the thread tables built above are indexed alongside the raw ones.
            for table in INDEXES:
                _index_one(con, table, present_tables(con))
    except Exception as exc:
        print(f"Warning: full-text index creation failed: {exc}", file=sys.stderr)


def _build_thread_table(
    con: duckdb.DuckDBPyConnection, name: str, present: set[str], usable: set[str]
) -> None:
    base, sql = THREAD_TABLES[name]
    key_column = INDEXES[name][0]
    if base not in present or key_column not in _present_columns(con, base):
        print(
            f"Note: skipping {name}; source table {base} is not present yet.",
            file=sys.stderr,
        )
        return
    try:
        con.execute(f"CREATE OR REPLACE TABLE github.{name} AS {render_slots(sql, usable)}")

        # CREATE OR REPLACE drops comments, so they are reapplied every time.
        con.execute(f"COMMENT ON TABLE github.{name} IS {quote_literal(THREAD_TABLE_DOCS[name])}")
        for column, doc in THREAD_COLUMN_DOCS[name].items():
            con.execute(f"COMMENT ON COLUMN github.{name}.{column} IS {quote_literal(doc)}")
    except Exception as exc:
        print(f"Warning: could not build {name}: {exc}", file=sys.stderr)


def _index_one(con: duckdb.DuckDBPyConnection, table: str, present: set[str]) -> None:
    key_column, columns = INDEXES[table]
    if table not in present:
        print(
            f"Note: skipping full-text index for {table}; the table is not present yet.",
            file=sys.stderr,
        )
        return
    try:
        available = _present_columns(con, table)
        if key_column not in available:
            print(
                f"Note: skipping full-text index for {table}; "
                f"key column {key_column} is not present.",
                file=sys.stderr,
            )
            return
        # dlt does not materialize a column that never received data, so index what is
        # actually there. The column set is reported by `schema` rather than assumed.
        indexed = [column for column in columns if column in available]
        if not indexed:
            print(
                f"Note: skipping full-text index for {table}; no text columns are present.",
                file=sys.stderr,
            )
            return
        if not _key_is_usable(con, table, key_column):
            print(
                f"Warning: skipping full-text index for {table}; "
                f"{key_column} is not unique and non-null, which would score rows wrongly.",
                file=sys.stderr,
            )
            return
        quoted = ", ".join(f"'{column}'" for column in indexed)
        con.execute(
            f"PRAGMA create_fts_index('github.{table}', '{key_column}', {quoted}, overwrite=1)"
        )
    except Exception as exc:
        print(f"Warning: could not build full-text index for {table}: {exc}", file=sys.stderr)

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from ghtriage.full_text_search import (
    INDEXES,
    THREAD_COLUMN_DOCS,
    THREAD_TABLE_DOCS,
    THREAD_TABLES,
    create_search_indexes,
    index_schema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# One shared corpus, written so BM25 ordering is predictable:
#
#   issues
#     1  'segfault on windows'  term in the title, one comment
#     2  'crash report'         term three times in the body -> outranks issue 1
#     3  'quiet issue'          term appears only in a comment on it
#     4  'no comments here'     nothing matches, and no comments at all
#   pull_requests
#     11 'fix segfault'         one conversation comment and one review comment
#     12 'docs update'          nothing matches
#
# `zzyzx` is a term that appears nowhere, for asserting empty results without
# relying on a real word being absent.


def _api(kind: str, number: int) -> str:
    return f"https://api.github.com/repos/someorg/somerepo/{kind}/{number}"


def _d(day: int) -> datetime:
    """A UTC-pinned fixture timestamp. Naive literals would inherit the local zone."""
    return datetime(2026, 1, day, tzinfo=timezone.utc)


def _create_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SCHEMA github")
    con.execute(
        "CREATE TABLE github.issues "
        "(id BIGINT, number BIGINT, title VARCHAR, body VARCHAR, _dlt_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE github.pull_requests "
        "(id BIGINT, number BIGINT, title VARCHAR, body VARCHAR, _dlt_id VARCHAR)"
    )
    con.execute(
        "CREATE TABLE github.conversation_comments "
        "(id BIGINT, issue_url VARCHAR, body VARCHAR, created_at TIMESTAMP WITH TIME ZONE)"
    )
    con.execute(
        "CREATE TABLE github.review_comments "
        "(id BIGINT, pull_request_url VARCHAR, body VARCHAR, created_at TIMESTAMP WITH TIME ZONE)"
    )


def _populate(con: duckdb.DuckDBPyConnection) -> None:
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?)",
        [
            (1001, 1, "segfault on windows", "crash report attached", "i1"),
            (1002, 2, "crash report", "segfault segfault segfault again", "i2"),
            (1003, 3, "quiet issue", "nothing to see here", "i3"),
            (1004, 4, "no comments here", "lonely body", "i4"),
        ],
    )
    con.executemany(
        "INSERT INTO github.pull_requests VALUES (?,?,?,?,?)",
        [
            (2011, 11, "fix segfault", "patches the crash", "p11"),
            (2012, 12, "docs update", "typo fix", "p12"),
        ],
    )
    con.executemany(
        "INSERT INTO github.conversation_comments VALUES (?,?,?,?)",
        [
            # Deliberately inserted newest-first: thread text must sort these.
            (3102, _api("issues", 3), "second word about it", _d(9)),
            (3101, _api("issues", 3), "the segfault reproduces for me", _d(5)),
            (3103, _api("issues", 1), "confirmed on my machine", _d(6)),
            (3104, _api("issues", 11), "reviewed the patch", _d(7)),
        ],
    )
    con.executemany(
        "INSERT INTO github.review_comments VALUES (?,?,?,?)",
        [
            (4201, _api("pulls", 11), "nit rename this variable", _d(8)),
        ],
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A fully populated database, as a repo with real history would look."""
    path = tmp_path / "full.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    _populate(con)
    con.close()
    return path


def _search(db_path: Path, table: str, query: str) -> list[tuple]:
    """Ranked (number, score) matches, exactly as the documented query form works."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute(
            f"SELECT number, score FROM ("  # noqa: S608
            f"  SELECT number, {index_schema(table)}.match_bm25(id, ?) AS score"
            f"  FROM github.{table}"
            f") WHERE score IS NOT NULL ORDER BY score DESC",
            [query],
        ).fetchall()


def _schemas(db_path: Path) -> set[str]:
    with duckdb.connect(str(db_path), read_only=True) as con:
        return {
            row[0]
            for row in con.execute(
                "SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE 'fts_%'"
            ).fetchall()
        }


# ---------------------------------------------------------------------------
# Index creation over the raw tables
# ---------------------------------------------------------------------------


def test_creates_index_for_every_declared_table(db: Path) -> None:
    create_search_indexes(db)

    assert _schemas(db) == {index_schema(table) for table in INDEXES}


def test_indexed_fields_match_declared_columns(db: Path) -> None:
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        for table, (_, columns) in INDEXES.items():
            indexed = [
                row[0]
                for row in con.execute(
                    f"SELECT field FROM {index_schema(table)}.fields ORDER BY fieldid"  # noqa: S608
                ).fetchall()
            ]
            assert indexed == list(columns), table


def test_declared_key_column_matches_index_documents(db: Path) -> None:
    """The declared key is the one actually indexed, pinned against the catalog.

    `docs.name` holds the key values the index was built from. Comparing it to the
    declared column is what stops INDEXES drifting from the built index.
    """
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        for table, (key_column, _) in INDEXES.items():
            keys = con.execute(
                f"SELECT {key_column} FROM github.{table} ORDER BY 1"  # noqa: S608
            ).fetchall()
            documents = con.execute(
                f"SELECT name FROM {index_schema(table)}.docs ORDER BY 1"  # noqa: S608
            ).fetchall()
            assert keys == documents, table


def test_search_finds_a_term_that_appears_only_in_the_body(db: Path) -> None:
    create_search_indexes(db)

    assert 2 in [number for number, _ in _search(db, "issues", "segfault")]


def test_search_ranks_repeated_term_above_single_mention(db: Path) -> None:
    """Issue 2 says 'segfault' three times; issue 1 says it once."""
    create_search_indexes(db)

    ranked = [number for number, _ in _search(db, "issues", "segfault")]
    assert ranked.index(2) < ranked.index(1)


def test_search_returns_no_rows_when_nothing_matches(db: Path) -> None:
    create_search_indexes(db)

    assert _search(db, "issues", "zzyzx") == []


# ---------------------------------------------------------------------------
# Thread tables
# ---------------------------------------------------------------------------


def _thread_text(db_path: Path, table: str, number: int) -> str:
    with duckdb.connect(str(db_path), read_only=True) as con:
        return con.execute(
            f"SELECT thread_text FROM github.{table} WHERE number = ?",  # noqa: S608
            [number],
        ).fetchone()[0]


@pytest.mark.parametrize(
    ("thread_table", "base_table"),
    [("issue_threads", "issues"), ("pull_request_threads", "pull_requests")],
)
def test_thread_table_has_one_row_per_base_row(
    db: Path, thread_table: str, base_table: str
) -> None:
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        threads, base = con.execute(
            f"SELECT (SELECT count(*) FROM github.{thread_table}), "  # noqa: S608
            f"(SELECT count(*) FROM github.{base_table})"
        ).fetchone()
    assert threads == base


def test_thread_text_includes_title_body_and_every_comment(db: Path) -> None:
    create_search_indexes(db)

    text = _thread_text(db, "issue_threads", 3)

    assert "quiet issue" in text
    assert "nothing to see here" in text
    assert "the segfault reproduces for me" in text
    assert "second word about it" in text


def test_thread_text_orders_comments_oldest_first(db: Path) -> None:
    create_search_indexes(db)

    text = _thread_text(db, "issue_threads", 3)

    assert text.index("the segfault reproduces for me") < text.index("second word about it")


def test_thread_text_is_title_and_body_when_there_are_no_comments(db: Path) -> None:
    create_search_indexes(db)

    assert _thread_text(db, "issue_threads", 4) == "no comments here\nlonely body"


def test_pull_request_thread_includes_both_comment_channels(db: Path) -> None:
    """Conversation and review comments live in different tables; a corpus wants both."""
    create_search_indexes(db)

    text = _thread_text(db, "pull_request_threads", 11)

    assert "reviewed the patch" in text
    assert "nit rename this variable" in text


def test_thread_search_finds_a_term_that_appears_only_in_a_comment(db: Path) -> None:
    """Issue 3 never says 'segfault' in its own title or body."""
    create_search_indexes(db)

    assert 3 in [number for number, _ in _search(db, "issue_threads", "segfault")]


# ---------------------------------------------------------------------------
# Where match_bm25 can be called from
# ---------------------------------------------------------------------------


def test_search_works_from_any_relation_carrying_the_key(db: Path) -> None:
    """The macro is keyed on the document id, not bound to the indexed table.

    This is what lets the derived views search without joining back to `issues`.
    """
    create_search_indexes(db)

    with duckdb.connect(str(db)) as con:
        con.execute("CREATE VIEW github.issue_summary AS SELECT id, number FROM github.issues")

    with duckdb.connect(str(db), read_only=True) as con:
        from_view = con.execute(
            "SELECT number FROM ("
            "  SELECT number, fts_github_issues.match_bm25(id, 'segfault') AS score"
            "  FROM github.issue_summary"
            ") WHERE score IS NOT NULL ORDER BY score DESC"
        ).fetchall()

    assert from_view == [(number,) for number, _ in _search(db, "issues", "segfault")]


def test_key_from_another_table_scores_no_rows(db: Path) -> None:
    """Pairing the wrong index with a table fails closed, silently -- hence the docs."""
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        matched = con.execute(
            "SELECT count(*) FROM ("
            "  SELECT fts_github_issues.match_bm25(id, 'segfault') AS score"
            "  FROM github.pull_requests"
            ") WHERE score IS NOT NULL"
        ).fetchone()[0]

    assert matched == 0


# ---------------------------------------------------------------------------
# Documentation and idempotence
# ---------------------------------------------------------------------------


def test_applies_table_and_column_comments(db: Path) -> None:
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        table_comment = con.execute(
            "SELECT comment FROM duckdb_tables() "
            "WHERE schema_name = 'github' AND table_name = 'issue_threads'"
        ).fetchone()[0]
        column_comment = con.execute(
            "SELECT comment FROM duckdb_columns() WHERE schema_name = 'github' "
            "AND table_name = 'issue_threads' AND column_name = 'thread_text'"
        ).fetchone()[0]

    assert table_comment == THREAD_TABLE_DOCS["issue_threads"]
    assert column_comment == THREAD_COLUMN_DOCS["issue_threads"]["thread_text"]


def test_reapplies_comments_after_replace(db: Path) -> None:
    """CREATE OR REPLACE TABLE drops comments, exactly as CREATE OR REPLACE VIEW does."""
    create_search_indexes(db)
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        comment = con.execute(
            "SELECT comment FROM duckdb_tables() "
            "WHERE schema_name = 'github' AND table_name = 'issue_threads'"
        ).fetchone()[0]

    assert comment == THREAD_TABLE_DOCS["issue_threads"]


def test_thread_docs_match_thread_columns(db: Path) -> None:
    """Every column has exactly one doc, and no doc outlives its column."""
    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        for name in THREAD_TABLES:
            columns = {
                row[0]
                for row in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'github' AND table_name = ?",
                    [name],
                ).fetchall()
            }
            assert columns == set(THREAD_COLUMN_DOCS[name]), name


def test_is_idempotent(db: Path) -> None:
    def snapshot() -> list[tuple]:
        with duckdb.connect(str(db), read_only=True) as con:
            rows = con.execute(
                "SELECT table_name, column_name, data_type, comment FROM duckdb_columns() "
                "WHERE schema_name = 'github' ORDER BY table_name, column_name"
            ).fetchall()
            documents = [
                (
                    table,
                    con.execute(
                        f"SELECT count(*) FROM {index_schema(table)}.docs"  # noqa: S608
                    ).fetchone()[0],
                )
                for table in INDEXES
            ]
        return rows + documents

    create_search_indexes(db)
    first = snapshot()
    create_search_indexes(db)

    assert snapshot() == first


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


def _drop(db_path: Path, statement: str) -> None:
    with duckdb.connect(str(db_path)) as con:
        con.execute(statement)


def test_skips_table_when_base_table_missing(db: Path, capsys: pytest.CaptureFixture) -> None:
    _drop(db, "DROP TABLE github.review_comments")

    create_search_indexes(db)

    assert index_schema("review_comments") not in _schemas(db)
    assert index_schema("issues") in _schemas(db)
    assert "review_comments" in capsys.readouterr().err


def test_indexes_present_subset_when_a_column_is_missing(db: Path) -> None:
    """dlt does not materialize a column that never received data."""
    _drop(db, "ALTER TABLE github.issues DROP COLUMN body")

    create_search_indexes(db)

    with duckdb.connect(str(db), read_only=True) as con:
        indexed = [
            row[0]
            for row in con.execute(f"SELECT field FROM {index_schema('issues')}.fields").fetchall()
        ]
    assert indexed == ["title"]


def test_skips_table_when_no_text_column_is_present(db: Path) -> None:
    _drop(db, "ALTER TABLE github.conversation_comments DROP COLUMN body")

    create_search_indexes(db)

    assert index_schema("conversation_comments") not in _schemas(db)


def test_thread_table_degrades_when_comment_table_is_missing(db: Path) -> None:
    _drop(db, "DROP TABLE github.conversation_comments")

    create_search_indexes(db)

    assert _thread_text(db, "issue_threads", 3) == "quiet issue\nnothing to see here"
    assert index_schema("issue_threads") in _schemas(db)


def test_thread_table_degrades_when_comment_body_column_is_missing(db: Path) -> None:
    """The table is present but carries no text, which is not the same as absent."""
    _drop(db, "ALTER TABLE github.conversation_comments DROP COLUMN body")

    create_search_indexes(db)

    assert _thread_text(db, "issue_threads", 3) == "quiet issue\nnothing to see here"


def test_thread_table_degrades_when_base_body_column_is_missing(db: Path) -> None:
    _drop(db, "ALTER TABLE github.issues DROP COLUMN body")

    create_search_indexes(db)

    assert _thread_text(db, "issue_threads", 4) == "no comments here"


def test_skips_thread_table_when_base_table_is_missing(db: Path) -> None:
    _drop(db, "DROP TABLE github.pull_requests")

    create_search_indexes(db)

    assert index_schema("pull_request_threads") not in _schemas(db)
    assert index_schema("issue_threads") in _schemas(db)


def test_skips_table_when_key_column_is_missing(db: Path) -> None:
    _drop(db, "ALTER TABLE github.issues DROP COLUMN id")

    create_search_indexes(db)

    assert index_schema("issues") not in _schemas(db)


def test_skips_table_when_key_is_not_unique(db: Path, capsys: pytest.CaptureFixture) -> None:
    """A duplicate key makes every sharing row report the first one's score, silently."""
    _drop(db, "INSERT INTO github.issues VALUES (1001, 5, 'duplicate id', 'body', 'i5')")

    create_search_indexes(db)

    assert index_schema("issues") not in _schemas(db)
    assert "issues" in capsys.readouterr().err


def test_skips_table_when_key_is_null(db: Path) -> None:
    """A NULL key scores NULL forever, so the row is silently unsearchable."""
    _drop(db, "INSERT INTO github.issues VALUES (NULL, 5, 'no id', 'body', 'i5')")

    create_search_indexes(db)

    assert index_schema("issues") not in _schemas(db)


def test_swallows_errors_from_one_table(
    db: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setitem(INDEXES, "issues", ("id", ("no_such_column",)))

    create_search_indexes(db)

    assert index_schema("pull_requests") in _schemas(db)
    assert capsys.readouterr().err != ""


def test_swallows_errors_when_database_is_unusable(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    missing = tmp_path / "nope" / "missing.duckdb"

    create_search_indexes(missing)

    assert "Warning" in capsys.readouterr().err

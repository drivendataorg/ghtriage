from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest

from ghtriage import full_text_search
from ghtriage.derived import create_derived
from ghtriage.full_text_search import INDEXES, create_search_indexes, index_schema

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
    # Built by hand rather than by `derived`, so this file tests indexing on its own.
    # What `derived` actually produces is pinned there, against the same INDEXES spec.
    con.execute("""
        CREATE TABLE github.issue_threads AS
        SELECT id, number, concat_ws(E'\n', title, body) AS thread_text FROM github.issues
    """)
    con.execute("""
        CREATE TABLE github.pull_request_threads AS
        SELECT id, number, concat_ws(E'\n', title, body) AS thread_text FROM github.pull_requests
    """)


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
# Index creation
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
# Idempotence
# ---------------------------------------------------------------------------


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
    """One table raising must not abort the tables after it.

    The failure has to come from the PRAGMA itself: a declared column that is merely
    absent is filtered out earlier, which is a different path.
    """
    real_check = full_text_search._key_is_usable

    def explode_for_issues(con, table, key_column):
        if table == "issues":
            raise RuntimeError("index build exploded")
        return real_check(con, table, key_column)

    monkeypatch.setattr(full_text_search, "_key_is_usable", explode_for_issues)

    create_search_indexes(db)

    assert index_schema("pull_requests") in _schemas(db)
    assert index_schema("issues") not in _schemas(db)
    assert "index build exploded" in capsys.readouterr().err


def test_swallows_errors_when_database_is_unusable(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    missing = tmp_path / "nope" / "missing.duckdb"

    create_search_indexes(missing)

    assert "Warning" in capsys.readouterr().err


def test_skipping_a_rebuild_drops_the_previous_index(db: Path) -> None:
    """A skipped build must leave no index, not yesterday's.

    `overwrite=1` only refreshes when the PRAGMA runs, so without an explicit drop a
    degraded pull would keep scoring against the previous pull's rows -- silently, and
    with a document count that looks plausible in `ghtriage schema`.
    """
    create_search_indexes(db)
    assert index_schema("issues") in _schemas(db)

    _drop(db, "ALTER TABLE github.issues DROP COLUMN title")
    _drop(db, "ALTER TABLE github.issues DROP COLUMN body")
    create_search_indexes(db)

    assert index_schema("issues") not in _schemas(db)


def test_skipping_a_rebuild_for_an_unusable_key_drops_the_previous_index(db: Path) -> None:
    create_search_indexes(db)

    _drop(db, "INSERT INTO github.issues VALUES (1001, 5, 'duplicate id', 'body', 'i5')")
    create_search_indexes(db)

    assert index_schema("issues") not in _schemas(db)


# ---------------------------------------------------------------------------
# Composition with `derived`
# ---------------------------------------------------------------------------


def test_indexes_the_thread_tables_that_derived_builds(tmp_path: Path) -> None:
    """The seam between the two modules: materialize, then index, then search.

    Every other test here builds the thread tables by hand. `run_pull` calls the real
    `create_derived` first, and only mocks stand between the two in test_pipeline, so
    without this nothing checks that the pair actually works together.
    """
    path = tmp_path / "composed.duckdb"
    con = duckdb.connect(str(path))
    _create_schema(con)
    con.executemany(
        "INSERT INTO github.issues VALUES (?,?,?,?,?)",
        [(1001, 1, "a quiet title", "an unremarkable body", "i1")],
    )
    con.executemany(
        "INSERT INTO github.conversation_comments VALUES (?,?,?,?)",
        [(3101, _api("issues", 1), "the zzyzx only ever appears in this comment", _d(5))],
    )
    con.close()

    create_derived(path)
    create_search_indexes(path)

    # Findable only if the thread table concatenated the comment and the index read it.
    assert [number for number, _ in _search(path, "issue_threads", "zzyzx")] == [1]

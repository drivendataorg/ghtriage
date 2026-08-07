from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from ghtriage.query import (
    StatusData,
    execute_query,
    get_status_data,
    get_table_columns,
    get_table_descriptions,
    get_tables,
)


@pytest.fixture
def sample_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github.issues (id BIGINT, title VARCHAR)")
    con.execute("INSERT INTO github.issues VALUES (1, 'A'), (2, 'B')")
    con.execute("CREATE TABLE github._dlt_loads (load_id VARCHAR)")
    con.execute("CREATE TABLE github.issues__labels (issue_id BIGINT, name VARCHAR)")
    con.close()

    return tmp_path


def test_execute_query_uses_github_schema(sample_cwd: Path) -> None:
    columns, rows = execute_query(
        "SELECT id, title FROM issues ORDER BY id",
        cwd=sample_cwd,
    )

    assert columns == ["id", "title"]
    assert rows == [(1, "A"), (2, "B")]


def test_execute_query_returns_empty_when_cursor_has_no_description(
    sample_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cursor = MagicMock()
    cursor.description = None

    connection = MagicMock()
    connection.execute.side_effect = [cursor, cursor]
    connection.__enter__.return_value = connection
    connection.__exit__.return_value = None

    monkeypatch.setattr("ghtriage.query.duckdb.connect", lambda *_args, **_kwargs: connection)

    columns, rows = execute_query(
        "CREATE TABLE write_probe (id BIGINT)",
        cwd=sample_cwd,
    )

    assert columns == []
    assert rows == []


def test_execute_query_raises_when_db_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Database not found"):
        execute_query("SELECT 1", cwd=tmp_path)


def test_execute_query_missing_db_has_no_side_effects(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Database not found"):
        execute_query("SELECT 1", cwd=tmp_path)

    assert not (tmp_path / ".ghtriage").exists()


def test_get_tables_hides_internal_by_default(sample_cwd: Path) -> None:
    assert get_tables(cwd=sample_cwd) == ["issues", "issues__labels"]


def test_get_tables_can_include_internal(sample_cwd: Path) -> None:
    assert get_tables(cwd=sample_cwd, include_internal=True) == [
        "_dlt_loads",
        "issues",
        "issues__labels",
    ]


def test_get_table_columns_returns_column_metadata(sample_cwd: Path) -> None:
    assert get_table_columns("issues", cwd=sample_cwd) == [
        ("id", "BIGINT", True, None),
        ("title", "VARCHAR", True, None),
    ]


@pytest.mark.parametrize(
    ("column_name", "expected_comment"),
    [
        ("title", "Title of the issue."),
        ("id", None),
    ],
)
def test_get_table_columns_returns_expected_comments(
    sample_cwd: Path, column_name: str, expected_comment: str | None
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("COMMENT ON COLUMN github.issues.title IS 'Title of the issue.'")

    columns = get_table_columns("issues", cwd=sample_cwd)
    selected_col = next(c for c in columns if c[0] == column_name)
    assert selected_col[3] == expected_comment


@pytest.mark.parametrize("add_comment", [False, True])
def test_get_table_descriptions_returns_expected_values(
    sample_cwd: Path, add_comment: bool
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    if not add_comment:
        assert get_table_descriptions(cwd=sample_cwd) == {}
        return

    with duckdb.connect(str(db_path)) as conn:
        conn.execute("COMMENT ON TABLE github.issues IS 'Issues track tasks and bugs.'")

    descriptions = get_table_descriptions(cwd=sample_cwd)
    assert descriptions["issues"] == "Issues track tasks and bugs."


def test_get_table_columns_raises_for_missing_table(sample_cwd: Path) -> None:
    with pytest.raises(ValueError, match="Table not found"):
        get_table_columns("does_not_exist", cwd=sample_cwd)


@pytest.fixture
def status_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github._ghtriage_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('repo', 'owner/testrepo')")
    con.execute(
        "INSERT INTO github._ghtriage_meta VALUES ('last_pull_at', '2026-02-28T14:22:57Z')"
    )
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('last_full_pull', 'false')")
    con.execute("CREATE TABLE github.issues (id BIGINT, updated_at TIMESTAMP)")
    con.execute("INSERT INTO github.issues VALUES (1, '2026-02-27 18:03:12')")
    con.execute("INSERT INTO github.issues VALUES (2, '2026-02-26 09:00:00')")
    con.execute("CREATE TABLE github.pulls (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.issue_comments (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.pull_comments (id BIGINT, updated_at TIMESTAMP)")
    con.close()

    return tmp_path


def test_get_status_data_returns_meta_fields(status_cwd: Path) -> None:
    status = get_status_data(cwd=status_cwd)

    assert isinstance(status, StatusData)
    assert status.db_repo == "owner/testrepo"
    assert status.last_pull_at == "2026-02-28T14:22:57Z"
    assert status.last_full_pull is False
    assert status.db_size_bytes > 0


def test_get_status_data_returns_table_stats(status_cwd: Path) -> None:
    status = get_status_data(cwd=status_cwd)

    table_names = [name for name, _, _ in status.table_stats]
    assert table_names == ["issues", "pulls", "issue_comments", "pull_comments"]

    issues_stats = next(s for s in status.table_stats if s[0] == "issues")
    assert issues_stats[1] == 2
    assert issues_stats[2] is not None and "2026-02-27" in issues_stats[2]

    pulls_stats = next(s for s in status.table_stats if s[0] == "pulls")
    assert pulls_stats[1] == 0
    assert pulls_stats[2] is None


def test_get_status_data_raises_when_db_missing(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Database not found"):
        get_status_data(cwd=tmp_path)


@pytest.fixture
def cwd_with_view(sample_cwd: Path) -> Path:
    """sample_cwd plus a derived view, as create_views() would leave it."""
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE VIEW github.issue_activity AS SELECT id, title FROM github.issues")
        conn.execute("COMMENT ON VIEW github.issue_activity IS 'Derived view: one row per issue.'")
    return sample_cwd


def test_get_tables_includes_views(cwd_with_view: Path) -> None:
    assert "issue_activity" in get_tables(cwd=cwd_with_view)


def test_get_table_descriptions_includes_views(cwd_with_view: Path) -> None:
    """duckdb_tables() excludes views, so a view description would be invisible."""
    descriptions = get_table_descriptions(cwd=cwd_with_view)

    assert descriptions["issue_activity"] == "Derived view: one row per issue."


def test_get_table_columns_works_on_views(cwd_with_view: Path) -> None:
    assert [name for name, _, _, _ in get_table_columns("issue_activity", cwd=cwd_with_view)] == [
        "id",
        "title",
    ]

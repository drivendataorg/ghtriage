import csv
import io
import json
from pathlib import Path
import re

import duckdb
import pytest

from ghtriage.cli import run


@pytest.fixture
def sample_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github.issues (id BIGINT, title VARCHAR, state VARCHAR)")
    con.execute("INSERT INTO github.issues VALUES (1, 'First', 'open'), (2, 'Second', 'closed')")
    con.close()

    return tmp_path


def test_query_table_format_success(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, title FROM issues ORDER BY id", "--format", "table"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "id" in captured.out
    assert "title" in captured.out
    assert "First" in captured.out
    assert captured.err == ""


def test_query_csv_format_success(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, title FROM issues ORDER BY id", "--format", "csv"])

    captured = capsys.readouterr()
    rows = list(csv.reader(io.StringIO(captured.out)))

    assert rc == 0
    assert rows[0] == ["id", "title"]
    assert rows[1] == ["1", "First"]
    assert rows[2] == ["2", "Second"]
    assert captured.err == ""
    assert "\r\n" not in captured.out


def test_query_json_format_is_strict_jsonl(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, state FROM issues ORDER BY id", "--format", "json"])

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]

    assert rc == 0
    assert payloads == [{"id": 1, "state": "open"}, {"id": 2, "state": "closed"}]
    assert captured.err == ""


def test_query_returns_runtime_error_for_missing_db(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    rc = run(["query", "SELECT 1"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Database not found" in captured.err


def test_query_returns_runtime_error_for_bad_sql(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELEC id FROM issues"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err


def test_query_rejects_write_sql_in_read_only_mode(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "CREATE TABLE write_probe (id BIGINT)"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Query failed" in captured.err


def test_schema_lists_user_tables(sample_cwd: Path, monkeypatch, capsys) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE github._dlt_loads (load_id VARCHAR)")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out.splitlines()
    assert "_dlt_loads" not in captured.out


def test_schema_unknown_table_returns_runtime_error(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "missing"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Table not found" in captured.err


def test_schema_table_details_shows_description_column_when_comments_present(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("COMMENT ON COLUMN github.issues.title IS 'Title of the issue.'")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "issues"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "description" in captured.out
    assert "Title of the issue." in captured.out


def test_schema_table_details_omits_description_column_when_no_comments(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "issues"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "description" not in captured.out
    assert "id" in captured.out
    assert "BIGINT" in captured.out


def test_schema_listing_shows_table_descriptions_when_present(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("COMMENT ON TABLE github.issues IS 'Issues track tasks and bugs.'")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out
    assert "Issues track tasks and bugs." in captured.out


def test_schema_listing_plain_when_no_table_descriptions(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out.splitlines()


@pytest.fixture
def status_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github._ghtriage_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('repo', 'owner/repo')")
    con.execute(
        "INSERT INTO github._ghtriage_meta VALUES ('last_pull_at', '2026-02-28T14:22:57Z')"
    )
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('last_full_pull', 'false')")
    con.execute("CREATE TABLE github.issues (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.pull_requests (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.conversation_comments (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.review_comments (id BIGINT, updated_at TIMESTAMP)")
    con.close()

    return tmp_path


def test_status_shows_db_info(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "owner/repo" in captured.out
    assert "2026-02-28" in captured.out
    assert "GITHUB_TOKEN" in captured.out
    assert captured.err == ""


def test_status_not_yet_pulled_without_db(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "not yet pulled" in captured.out
    assert captured.err == ""


def test_status_shows_mismatch_warning(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/other-repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "WARNING" in captured.out
    assert "owner/other-repo" in captured.out
    assert "owner/repo" in captured.out


def test_status_handles_missing_config_repo(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        "ghtriage.cli.resolve_repo", lambda: (_ for _ in ()).throw(RuntimeError("no remote"))
    )

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "unknown" in captured.out


@pytest.fixture
def cwd_with_view(sample_cwd: Path) -> Path:
    """sample_cwd plus a documented derived view, as create_views() would leave it."""
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE VIEW github.issue_activity AS SELECT id, title FROM github.issues")
        con.execute("COMMENT ON VIEW github.issue_activity IS 'Derived view: one row per issue.'")
        con.execute("COMMENT ON COLUMN github.issue_activity.id IS 'Pass-through of issues.id.'")
    return sample_cwd


def test_schema_listing_shows_view_description(cwd_with_view: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_view)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "issue_activity" in out
    assert "Derived view: one row per issue." in out


def test_schema_table_details_works_on_a_view(cwd_with_view: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_view)

    rc = run(["schema", "--table", "issue_activity"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Pass-through of issues.id." in out


@pytest.fixture
def cwd_with_index(sample_cwd: Path) -> Path:
    """sample_cwd plus a full-text index, as create_search_indexes() would leave it."""
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("PRAGMA create_fts_index('github.issues', 'id', 'title', overwrite=1)")
    return sample_cwd


def test_schema_listing_shows_full_text_indexes(cwd_with_index: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_index)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Full-text search indexes" in out
    assert "fts_github_issues.match_bm25" in out
    # The index's own row: columns and document count as built, not as declared.
    # Matched together so neither can be satisfied by text elsewhere in the output.
    assert re.search(r"issues\s*\|\s*id\s*\|\s*title\s*\|\s*2\b", out)


def test_schema_listing_omits_index_block_when_there_are_none(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Full-text search" not in out


def test_schema_listing_warns_that_a_wrong_index_returns_no_rows(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    """The failure mode is silent, so the only place it can be caught is here."""
    monkeypatch.chdir(cwd_with_index)

    run(["schema"])

    out = capsys.readouterr().out
    assert "almost always returns no rows" in out
    assert "colliding id" in out


def test_schema_table_details_shows_the_index(cwd_with_index: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_index)

    rc = run(["schema", "--table", "issues"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "fts_github_issues.match_bm25(id, 'query') over title (2 documents)" in out


def test_schema_table_details_omits_index_line_for_unindexed_table(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    db_path = cwd_with_index / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE github.other (id BIGINT)")
    monkeypatch.chdir(cwd_with_index)

    run(["schema", "--table", "other"])

    assert "match_bm25" not in capsys.readouterr().out


def test_schema_index_example_uses_the_first_declared_table(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    """Listing is alphabetical, but a comment table is a poor thing to demonstrate with."""
    db_path = cwd_with_index / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE github.conversation_comments (id BIGINT, body VARCHAR)")
        con.execute("INSERT INTO github.conversation_comments VALUES (1, 'a comment')")
        con.execute(
            "PRAGMA create_fts_index('github.conversation_comments', 'id', 'body', overwrite=1)"
        )
    monkeypatch.chdir(cwd_with_index)

    run(["schema"])

    out = capsys.readouterr().out
    assert "SELECT *, fts_github_issues.match_bm25(id, 'search terms') AS score" in out

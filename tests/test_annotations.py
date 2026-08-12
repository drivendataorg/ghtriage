import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

import duckdb
import pytest

from ghtriage.annotations import (
    OPENAPI_FETCH_TIMEOUT_SECONDS,
    _extract_descriptions,
    _resolve_ref,
    annotate_database,
    annotate_propagated_keys,
    build_column_descriptions,
    build_table_descriptions,
    fetch_and_annotate,
    fetch_spec,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def minimal_spec() -> dict:
    """A minimal OpenAPI spec with the structure needed for testing."""
    return {
        "components": {
            "schemas": {
                "issue": {
                    "description": "An issue on a GitHub repository.",
                    "properties": {
                        "number": {"description": "Number uniquely identifying the issue."},
                        "title": {"description": "Title of the issue."},
                        "state": {"description": "State of the issue; either 'open' or 'closed'."},
                        "body": {},  # no description
                    },
                },
                "pull-request-simple": {
                    "description": "A simplified pull request.",
                    "properties": {
                        "draft": {"description": "Indicates whether the pull request is a draft."},
                    },
                },
                "issue-comment": {
                    "description": "A comment on an issue.",
                    "properties": {
                        "body": {"description": "Contents of the issue comment"},
                    },
                },
                "pull-request-review-comment": {
                    "description": "A review comment on a pull request.",
                    "properties": {
                        "body": {"description": "The text of the comment."},
                    },
                },
                "simple-user": {
                    "properties": {
                        "login": {"description": "The GitHub username."},
                        "id": {},
                    }
                },
            }
        }
    }


@pytest.fixture
def annotated_db(tmp_path: Path) -> Path:
    """A DuckDB file with a github schema and an issues table."""
    db_path = tmp_path / "test.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA github")
        conn.execute("CREATE TABLE github.issues (id BIGINT, title VARCHAR, state VARCHAR)")
    return db_path


# ---------------------------------------------------------------------------
# fetch_spec
# ---------------------------------------------------------------------------


def test_fetch_spec_success() -> None:
    payload = {"openapi": "3.0.3", "components": {}}
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps(payload).encode()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    with patch(
        "ghtriage.annotations.urllib.request.urlopen", return_value=mock_response
    ) as mock_urlopen:
        result = fetch_spec("https://example.com/spec.json")

    assert result == payload
    mock_urlopen.assert_called_once_with(
        "https://example.com/spec.json",
        timeout=OPENAPI_FETCH_TIMEOUT_SECONDS,
    )


def test_fetch_spec_http_error() -> None:
    http_error = urllib.error.HTTPError(
        url="https://example.com/spec.json",
        code=404,
        msg="Not Found",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,  # type: ignore[arg-type]
    )
    with patch("ghtriage.annotations.urllib.request.urlopen", side_effect=http_error):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            fetch_spec("https://example.com/spec.json")


# ---------------------------------------------------------------------------
# _resolve_ref
# ---------------------------------------------------------------------------


def test_resolve_ref_no_ref() -> None:
    schema = {"type": "string", "description": "A string"}
    spec = {}
    assert _resolve_ref(schema, spec) == schema


def test_resolve_ref_resolves_component() -> None:
    spec = {"components": {"schemas": {"Foo": {"type": "object", "properties": {}}}}}
    schema = {"$ref": "#/components/schemas/Foo"}
    result = _resolve_ref(schema, spec)
    assert result == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# _extract_descriptions
# ---------------------------------------------------------------------------


def test_extract_descriptions_scalar(minimal_spec: dict) -> None:
    schema = minimal_spec["components"]["schemas"]["issue"]
    result = _extract_descriptions(schema, minimal_spec)
    assert result["number"] == "Number uniquely identifying the issue."
    assert result["title"] == "Title of the issue."
    assert result["state"] == "State of the issue; either 'open' or 'closed'."
    assert "body" not in result  # no description on body


def test_extract_descriptions_array_not_recursed() -> None:
    spec = {"components": {"schemas": {}}}
    schema = {
        "properties": {
            "labels": {
                "type": "array",
                "description": "Labels on the issue.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"description": "Label name."},
                    },
                },
            }
        }
    }
    result = _extract_descriptions(schema, spec)
    # Top-level description on the array field itself is captured
    assert result["labels"] == "Labels on the issue."
    # But nested items are not recursed into
    assert "labels__name" not in result


def test_extract_descriptions_multi_level_ref() -> None:
    """$ref chains are resolved at each recursive level, not just the top level."""
    spec = {
        "components": {
            "schemas": {
                "creator": {
                    "properties": {
                        "login": {"description": "Creator login."},
                    }
                },
                "milestone": {
                    "properties": {
                        "creator": {"$ref": "#/components/schemas/creator"},
                    }
                },
            }
        }
    }
    schema = {
        "properties": {
            "milestone": {"$ref": "#/components/schemas/milestone"},
        }
    }
    result = _extract_descriptions(schema, spec)
    assert result["milestone__creator__login"] == "Creator login."


# ---------------------------------------------------------------------------
# build_column_descriptions / build_table_descriptions
# ---------------------------------------------------------------------------


def test_build_column_descriptions_structure(minimal_spec: dict) -> None:
    result = build_column_descriptions(minimal_spec)
    assert set(result.keys()) == {
        "issues",
        "pull_requests",
        "conversation_comments",
        "review_comments",
    }
    assert "number" in result["issues"]
    assert "draft" in result["pull_requests"]


def test_build_column_descriptions_flattens_inline_nested_object() -> None:
    spec = {
        "components": {
            "schemas": {
                "issue": {
                    "properties": {
                        "user": {
                            "type": "object",
                            "properties": {
                                "login": {"description": "The GitHub username."},
                            },
                        }
                    }
                },
                "pull-request-simple": {"properties": {}},
                "issue-comment": {"properties": {}},
                "pull-request-review-comment": {"properties": {}},
            }
        }
    }
    result = build_column_descriptions(spec)
    assert result["issues"]["user__login"] == "The GitHub username."


def test_build_table_descriptions_structure(minimal_spec: dict) -> None:
    result = build_table_descriptions(minimal_spec)
    assert set(result.keys()) == {
        "issues",
        "pull_requests",
        "conversation_comments",
        "review_comments",
    }
    assert "issue" in result["issues"].lower()
    assert "pull request" in result["pull_requests"].lower()


# ---------------------------------------------------------------------------
# annotate_database
# ---------------------------------------------------------------------------


def test_annotate_database_applies_column_comments(annotated_db: Path) -> None:
    column_descs = {"issues": {"title": "Title of the issue.", "state": "open or closed"}}
    annotate_database(annotated_db, {}, column_descs)

    with duckdb.connect(str(annotated_db)) as conn:
        comments = dict(
            conn.execute(
                "SELECT column_name, comment FROM duckdb_columns() "
                "WHERE schema_name = 'github' AND table_name = 'issues'"
            ).fetchall()
        )

    assert comments["title"] == "Title of the issue."
    assert comments["state"] == "open or closed"
    assert comments["id"] is None


def test_annotate_database_applies_table_comments(annotated_db: Path) -> None:
    table_descs = {"issues": "Issues are a great way to track tasks."}
    annotate_database(annotated_db, table_descs, {})

    with duckdb.connect(str(annotated_db)) as conn:
        row = conn.execute(
            "SELECT comment FROM duckdb_tables() "
            "WHERE schema_name = 'github' AND table_name = 'issues'"
        ).fetchone()

    assert row[0] == "Issues are a great way to track tasks."


def test_annotate_database_escapes_single_quotes(annotated_db: Path) -> None:
    column_descs = {"issues": {"state": "Either 'open' or 'closed'."}}
    annotate_database(annotated_db, {}, column_descs)

    with duckdb.connect(str(annotated_db)) as conn:
        row = conn.execute(
            "SELECT comment FROM duckdb_columns() "
            "WHERE schema_name = 'github' AND table_name = 'issues' AND column_name = 'state'"
        ).fetchone()

    assert row[0] == "Either 'open' or 'closed'."


def test_annotate_database_skips_missing_table(annotated_db: Path) -> None:
    """Tables absent from the database are silently skipped."""
    column_descs = {"pull_requests": {"title": "PR title"}}
    table_descs = {"pull_requests": "Pull requests."}
    # Should not raise even though 'pulls' table does not exist
    annotate_database(annotated_db, table_descs, column_descs)


def test_annotate_database_skips_missing_column(annotated_db: Path) -> None:
    """Columns absent from the table are silently skipped."""
    column_descs = {"issues": {"nonexistent_column": "Some description"}}
    annotate_database(annotated_db, {}, column_descs)  # should not raise


# ---------------------------------------------------------------------------
# fetch_and_annotate
# ---------------------------------------------------------------------------


def test_fetch_and_annotate_applies_comments_from_spec(annotated_db: Path) -> None:
    spec = {
        "components": {
            "schemas": {
                "issue": {
                    "description": "An issue table.",
                    "properties": {
                        "title": {"description": "Issue title."},
                        "state": {"description": "Either 'open' or 'closed'."},
                    },
                },
                "pull-request-simple": {"description": "A pull request.", "properties": {}},
                "issue-comment": {"description": "An issue comment.", "properties": {}},
                "pull-request-review-comment": {
                    "description": "A PR review comment.",
                    "properties": {},
                },
            }
        }
    }
    with patch("ghtriage.annotations.fetch_spec", return_value=spec):
        fetch_and_annotate(annotated_db)

    with duckdb.connect(str(annotated_db)) as conn:
        table_comment = conn.execute(
            "SELECT comment FROM duckdb_tables() "
            "WHERE schema_name = 'github' AND table_name = 'issues'"
        ).fetchone()
        column_comments = dict(
            conn.execute(
                "SELECT column_name, comment FROM duckdb_columns() "
                "WHERE schema_name = 'github' AND table_name = 'issues'"
            ).fetchall()
        )

    assert table_comment is not None
    assert table_comment[0] == "An issue table."
    assert column_comments["title"] == "Issue title."
    assert column_comments["state"] == "Either 'open' or 'closed'."


def test_fetch_and_annotate_propagates_errors(annotated_db: Path) -> None:
    """Whether a pull survives this is `run_pull`'s call, so the failure has to reach it."""
    with (
        patch("ghtriage.annotations.fetch_spec", side_effect=RuntimeError("network error")),
        pytest.raises(RuntimeError, match="network error"),
    ):
        fetch_and_annotate(annotated_db)


# ---------------------------------------------------------------------------
# annotate_propagated_keys
# ---------------------------------------------------------------------------


@pytest.fixture
def child_table_db(tmp_path: Path) -> Path:
    """A database holding the child tables dlt propagates parent keys into."""
    db_path = tmp_path / "children.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA github")
        conn.execute("CREATE TABLE github.issues (id BIGINT, number BIGINT)")
        conn.execute("CREATE TABLE github.issues__labels (issue_number BIGINT, name VARCHAR)")
        conn.execute(
            "CREATE TABLE github.issues__performed_via_github_app__events "
            "(issue_number BIGINT, name VARCHAR)"
        )
        conn.execute(
            "CREATE TABLE github.pull_requests__labels (pull_request_number BIGINT, name VARCHAR)"
        )
        conn.execute(
            "CREATE TABLE github.conversation_comments__reactions "
            "(comment_id BIGINT, content VARCHAR)"
        )
    return db_path


@pytest.mark.parametrize(
    ("table", "column", "join_target"),
    [
        ("issues__labels", "issue_number", "issues.number"),
        ("issues__performed_via_github_app__events", "issue_number", "issues.number"),
        ("pull_requests__labels", "pull_request_number", "pull_requests.number"),
        ("conversation_comments__reactions", "comment_id", "conversation_comments.id"),
    ],
)
def test_annotate_propagated_keys_documents_the_join(
    child_table_db: Path, table: str, column: str, join_target: str
) -> None:
    """These columns exist to be the documented join, so `schema` has to explain them."""
    annotate_propagated_keys(child_table_db)

    with duckdb.connect(str(child_table_db)) as conn:
        row = conn.execute(
            "SELECT comment FROM duckdb_columns() WHERE schema_name = 'github' "
            "AND table_name = ? AND column_name = ?",
            [table, column],
        ).fetchone()

    assert row[0] is not None and join_target in row[0]


def test_annotate_propagated_keys_skips_absent_tables_and_columns(tmp_path: Path) -> None:
    """A young or sparse repository has few of these tables, and dlt only adds a column
    once a record carrying it arrives -- the same contract as `annotate_database`."""
    db_path = tmp_path / "sparse.duckdb"
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA github")
        conn.execute("CREATE TABLE github.issues__labels (name VARCHAR)")

    annotate_propagated_keys(db_path)  # should not raise

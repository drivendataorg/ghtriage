from pathlib import Path
from unittest.mock import Mock

import dlt
from dlt.common.normalizers.json.relational import DataItemNormalizer
from dlt.common.schema import Schema
import duckdb
import pytest

from ghtriage.full_text_search import INDEXES
from ghtriage.pipeline import (
    KEY_PROPAGATION,
    SCHEMA_GENERATION,
    SchemaGenerationMismatch,
    _write_meta,
    build_rest_api_source,
    run_pull,
)


def _stub_source():
    """A stand-in for the constructed source, carrying a real schema.

    `build_rest_api_source` writes the key-propagation config onto the source's schema,
    so the stand-in needs one that config can be validated against.
    """
    return Mock(schema=Schema("ghtriage"))


def _install_pipeline_mocks(monkeypatch):
    sentinel_destination = object()
    sentinel_source = _stub_source()
    sentinel_run_result = object()

    mock_duckdb_factory = Mock(return_value=sentinel_destination)
    mock_pipeline_obj = Mock()
    mock_pipeline_obj.run = Mock(return_value=sentinel_run_result)
    mock_pipeline_factory = Mock(return_value=mock_pipeline_obj)
    mock_rest_api_source = Mock(return_value=sentinel_source)

    monkeypatch.setattr("ghtriage.pipeline.dlt.destinations.duckdb", mock_duckdb_factory)
    monkeypatch.setattr("ghtriage.pipeline.dlt.pipeline", mock_pipeline_factory)
    monkeypatch.setattr("ghtriage.pipeline.rest_api_source", mock_rest_api_source)
    mock_write_meta = Mock()
    monkeypatch.setattr("ghtriage.pipeline._write_meta", mock_write_meta)
    call_order: list[str] = []

    def records(step: str, result=None):
        """Note that the step ran, and return what the real one returns."""

        def run(*_args, **_kwargs):
            call_order.append(step)
            return result

        return run

    # The two builders return a list of per-object failures; the others return nothing.
    mock_create_derived = Mock(side_effect=records("create_derived", []))
    monkeypatch.setattr("ghtriage.pipeline.create_derived", mock_create_derived)
    mock_create_search_indexes = Mock(side_effect=records("create_search_indexes", []))
    monkeypatch.setattr("ghtriage.pipeline.create_search_indexes", mock_create_search_indexes)
    mock_fetch_and_annotate = Mock(side_effect=records("fetch_and_annotate"))
    monkeypatch.setattr("ghtriage.pipeline.fetch_and_annotate", mock_fetch_and_annotate)
    monkeypatch.setattr(
        "ghtriage.pipeline.annotate_propagated_keys",
        Mock(side_effect=records("annotate_propagated_keys")),
    )

    return (
        sentinel_destination,
        sentinel_source,
        sentinel_run_result,
        mock_duckdb_factory,
        mock_pipeline_obj,
        mock_pipeline_factory,
        mock_rest_api_source,
        mock_write_meta,
        mock_fetch_and_annotate,
        mock_create_derived,
        mock_create_search_indexes,
        call_order,
    )


def test_run_pull_smoke_full_false_calls_pipeline_run_once(tmp_path: Path, monkeypatch) -> None:
    (
        sentinel_destination,
        sentinel_source,
        sentinel_run_result,
        mock_duckdb_factory,
        mock_pipeline_obj,
        mock_pipeline_factory,
        mock_rest_api_source,
        mock_write_meta,
        mock_fetch_and_annotate,
        mock_create_derived,
        _mock_create_search_indexes,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    load_info, warnings = run_pull(repo="owner/repo", token="tok", full=False, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert warnings == []
    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)

    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    pipelines_dir = tmp_path / ".ghtriage" / "pipelines"

    mock_duckdb_factory.assert_called_once_with(str(db_path))
    mock_pipeline_factory.assert_called_once_with(
        pipeline_name="ghtriage",
        destination=sentinel_destination,
        dataset_name="github",
        pipelines_dir=str(pipelines_dir),
    )

    mock_rest_api_source.assert_called_once()
    config = mock_rest_api_source.call_args.args[0]
    resource_names = [resource["name"] for resource in config["resources"]]
    assert resource_names == [
        "issues",
        "pull_requests",
        "conversation_comments",
        "review_comments",
    ]

    mock_write_meta.assert_called_once_with(db_path=db_path, repo="owner/repo", full=False)
    mock_fetch_and_annotate.assert_called_once_with(db_path)


def test_run_pull_full_true_removes_existing_state_then_runs(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        sentinel_source,
        sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    ghtriage_dir = tmp_path / ".ghtriage"
    pipelines_dir = ghtriage_dir / "pipelines"
    old_pipeline_file = pipelines_dir / "stale" / "marker.txt"
    old_db_path = ghtriage_dir / "ghtriage.duckdb"

    old_pipeline_file.parent.mkdir(parents=True, exist_ok=True)
    old_pipeline_file.write_text("old", encoding="utf-8")
    old_db_path.write_text("old", encoding="utf-8")

    load_info, warnings = run_pull(repo="owner/repo", token="tok", full=True, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert warnings == []
    assert not old_db_path.exists()
    assert not old_pipeline_file.exists()
    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)


def test_run_pull_full_true_handles_missing_state(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        sentinel_source,
        sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    load_info, warnings = run_pull(repo="owner/repo", token="tok", full=True, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert warnings == []
    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)


def test_run_pull_builds_source_with_repo_and_token(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        _sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        _mock_pipeline_obj,
        _mock_pipeline_factory,
        mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    run_pull(repo="abc/def", token="secret", full=False, cwd=tmp_path)

    config = mock_rest_api_source.call_args.args[0]
    assert config["client"]["base_url"] == "https://api.github.com/repos/abc/def/"
    assert config["client"]["auth"]["token"] == "secret"


def test_write_meta_upserts_expected_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "test.duckdb"
    _write_meta(db_path=db_path, repo="owner/repo", full=False)

    with duckdb.connect(str(db_path)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM github._ghtriage_meta").fetchall())

    assert meta["repo"] == "owner/repo"
    assert meta["last_full_pull"] == "false"
    assert "T" in meta["last_pull_at"] and meta["last_pull_at"].endswith("Z")


def test_write_meta_records_full_flag(tmp_path: Path) -> None:
    db_path = tmp_path / "test.duckdb"
    _write_meta(db_path=db_path, repo="owner/repo", full=True)

    with duckdb.connect(str(db_path)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM github._ghtriage_meta").fetchall())

    assert meta["last_full_pull"] == "true"


def test_write_meta_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "test.duckdb"
    _write_meta(db_path=db_path, repo="owner/repo-a", full=False)
    _write_meta(db_path=db_path, repo="owner/repo-b", full=True)

    with duckdb.connect(str(db_path)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM github._ghtriage_meta").fetchall())

    assert meta["repo"] == "owner/repo-b"
    assert meta["last_full_pull"] == "true"


def test_write_meta_stamps_the_current_schema_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.duckdb"
    _write_meta(db_path=db_path, repo="owner/repo", full=False)

    with duckdb.connect(str(db_path)) as conn:
        meta = dict(conn.execute("SELECT key, value FROM github._ghtriage_meta").fetchall())

    assert meta["schema_generation"] == str(SCHEMA_GENERATION)


# ---------------------------------------------------------------------------
# An incremental pull refuses a database of another schema generation
# ---------------------------------------------------------------------------


def _make_database(cwd: Path, meta: dict[str, str] | None) -> Path:
    """A database as an earlier pull left it; `meta=None` predates the meta table."""
    db_path = cwd / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA github")
        if meta is not None:
            conn.execute(
                "CREATE TABLE github._ghtriage_meta (key VARCHAR PRIMARY KEY, value VARCHAR)"
            )
            for key, value in meta.items():
                conn.execute("INSERT INTO github._ghtriage_meta VALUES (?, ?)", [key, value])
    return db_path


@pytest.mark.parametrize(
    "meta",
    [
        pytest.param({"schema_generation": str(SCHEMA_GENERATION + 1)}, id="other_generation"),
        pytest.param({"repo": "owner/repo"}, id="meta_without_the_key"),
        pytest.param(None, id="no_meta_table"),
    ],
)
def test_run_pull_refuses_an_incremental_pull_into_another_generation(
    tmp_path: Path, monkeypatch, meta: dict[str, str] | None
) -> None:
    """Refusal happens before the load, so the database is never left mixed.

    A missing meta table or a meta table without the key reads as generation 0 --
    which is every database written before the stamp existed.
    """
    (
        _sentinel_destination,
        _sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    _make_database(tmp_path, meta)

    with pytest.raises(SchemaGenerationMismatch):
        run_pull(repo="owner/repo", token="t", full=False, cwd=tmp_path)

    mock_pipeline_obj.run.assert_not_called()


def test_schema_generation_mismatch_names_both_generations_and_the_fix(
    tmp_path: Path, monkeypatch
) -> None:
    _install_pipeline_mocks(monkeypatch)
    _make_database(tmp_path, {"repo": "owner/repo"})

    with pytest.raises(SchemaGenerationMismatch) as exc_info:
        run_pull(repo="owner/repo", token="t", full=False, cwd=tmp_path)

    message = str(exc_info.value)
    assert "generation 0" in message
    assert f"generation {SCHEMA_GENERATION}" in message
    assert "ghtriage pull --full" in message


def test_run_pull_full_does_not_check_the_generation(tmp_path: Path, monkeypatch) -> None:
    """`--full` deletes the database first, so there is no shape left to be stale."""
    (
        _sentinel_destination,
        sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    _make_database(tmp_path, {"schema_generation": str(SCHEMA_GENERATION + 1)})

    run_pull(repo="owner/repo", token="t", full=True, cwd=tmp_path)

    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)


def test_run_pull_does_not_check_the_generation_without_a_database(
    tmp_path: Path, monkeypatch
) -> None:
    """A first pull creates the database at the current generation; nothing can be stale."""
    (
        _sentinel_destination,
        sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    run_pull(repo="owner/repo", token="t", full=False, cwd=tmp_path)

    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)


def test_run_pull_proceeds_when_the_generation_matches(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        _mock_create_search_indexes,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    _make_database(tmp_path, {"schema_generation": str(SCHEMA_GENERATION)})

    run_pull(repo="owner/repo", token="t", full=False, cwd=tmp_path)

    mock_pipeline_obj.run.assert_called_once_with(sentinel_source)


def test_run_pull_creates_derived_before_annotating(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        _sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        _mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        mock_create_derived,
        _mock_create_search_indexes,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=False)

    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    mock_create_derived.assert_called_once_with(db_path)
    assert call_order == [
        "create_derived",
        "create_search_indexes",
        "fetch_and_annotate",
        "annotate_propagated_keys",
    ]


def test_run_pull_creates_derived_on_full_rebuild(tmp_path: Path, monkeypatch) -> None:
    (
        _sentinel_destination,
        _sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        _mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        mock_create_derived,
        _mock_create_search_indexes,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=True)

    mock_create_derived.assert_called_once()
    assert call_order == [
        "create_derived",
        "create_search_indexes",
        "fetch_and_annotate",
        "annotate_propagated_keys",
    ]


def test_run_pull_builds_search_indexes_between_derived_and_annotation(
    tmp_path: Path, monkeypatch
) -> None:
    (
        _sentinel_destination,
        _sentinel_source,
        _sentinel_run_result,
        _mock_duckdb_factory,
        _mock_pipeline_obj,
        _mock_pipeline_factory,
        _mock_rest_api_source,
        _mock_write_meta,
        _mock_fetch_and_annotate,
        _mock_create_derived,
        mock_create_search_indexes,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=False)

    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    mock_create_search_indexes.assert_called_once_with(db_path)
    assert call_order == [
        "create_derived",
        "create_search_indexes",
        "fetch_and_annotate",
        "annotate_propagated_keys",
    ]


# ---------------------------------------------------------------------------
# A pull survives every decorating step failing
# ---------------------------------------------------------------------------
#
# The build steps raise; whether that costs the pull is decided here, which is why
# these live with the orchestrator rather than with the modules that raise.


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("ghtriage.pipeline._write_meta", "metadata write failed"),
        ("ghtriage.pipeline.create_derived", "derived objects failed"),
        ("ghtriage.pipeline.create_search_indexes", "full-text indexes failed"),
        ("ghtriage.pipeline.fetch_and_annotate", "schema annotation failed"),
        ("ghtriage.pipeline.annotate_propagated_keys", "join key documentation failed"),
    ],
)
def test_run_pull_survives_and_reports_a_failed_step(
    tmp_path: Path, monkeypatch, target: str, expected: str
) -> None:
    _install_pipeline_mocks(monkeypatch)
    monkeypatch.setattr(target, Mock(side_effect=RuntimeError("boom")))
    monkeypatch.chdir(tmp_path)

    _load_info, warnings = run_pull(repo="owner/repo", token="t", full=False)

    assert [w for w in warnings if w.startswith(expected)]
    assert "boom" in warnings[0]


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("ghtriage.pipeline.create_derived", "could not create view issue_activity"),
        ("ghtriage.pipeline.create_search_indexes", "could not build the full-text index"),
    ],
)
def test_run_pull_reports_a_single_object_that_failed_to_build(
    tmp_path: Path, monkeypatch, target: str, expected: str
) -> None:
    """One object failing is not a failed step, but it is still something to report.

    Left only on stderr it would be invisible to `pull`'s exit path and to any caller
    reading the return value.
    """
    _install_pipeline_mocks(monkeypatch)
    monkeypatch.setattr(target, Mock(return_value=[expected]))
    monkeypatch.chdir(tmp_path)

    _load_info, warnings = run_pull(repo="owner/repo", token="t", full=False)

    assert expected in warnings


# ---------------------------------------------------------------------------
# The loader guarantee the full-text indexes rest on
# ---------------------------------------------------------------------------


def test_every_indexed_resource_merges_on_id(monkeypatch) -> None:
    """`id` is the full-text document key, and dlt's merge is what makes it unique.

    Nothing re-verifies that at index time: this is the layer that provides the
    guarantee, so this is the layer that gets the test.
    """
    captured: dict = {}

    def capture(config):
        captured.update(config)
        return _stub_source()

    monkeypatch.setattr("ghtriage.pipeline.rest_api_source", capture)

    build_rest_api_source(repo="owner/repo", token="t")

    defaults = captured["resource_defaults"]
    for resource in captured["resources"]:
        assert resource.get("primary_key", defaults["primary_key"]) == "id", resource["name"]
        assert resource.get("write_disposition", defaults["write_disposition"]) == "merge", (
            resource["name"]
        )

    # Every raw table an index keys on `id` is one of these resources.
    declared = {name for name in INDEXES if not name.endswith("_threads")}
    assert declared == {resource["name"] for resource in captured["resources"]}


# ---------------------------------------------------------------------------
# The GitHub join keys child tables carry
# ---------------------------------------------------------------------------


def test_source_schema_declares_key_propagation() -> None:
    """The documented child-table join is a normalizer setting, not something the
    resources produce, so the declaration is where it gets pinned."""
    source = build_rest_api_source(repo="owner/repo", token="t")

    json_config = source.schema.to_dict()["normalizers"]["json"]["config"]
    assert json_config["propagation"]["tables"] == KEY_PROPAGATION


def _run_fixture_pipeline(tmp_path: Path, records: list[dict]) -> Path:
    """Load fixture records through a real dlt pipeline with the propagation config.

    Hermetic: dicts in, DuckDB out, no HTTP. The child tables only exist because the
    normalizer makes them, so this is the only way to see what it puts in them.
    """

    @dlt.source(name="fixtures")
    def fixture_source():
        @dlt.resource(name="issues", primary_key="id", write_disposition="merge")
        def issues():
            yield from records

        return issues

    # The config the real source carries, so this exercises the wiring and not just dlt.
    built = build_rest_api_source(repo="owner/repo", token="t")
    source = fixture_source()
    DataItemNormalizer.update_normalizer_config(
        source.schema, built.schema.to_dict()["normalizers"]["json"].get("config", {})
    )

    db_path = tmp_path / "fixtures.duckdb"
    pipeline = dlt.pipeline(
        pipeline_name="ghtriage_fixtures",
        destination=dlt.destinations.duckdb(str(db_path)),
        dataset_name="github",
        pipelines_dir=str(tmp_path / "pipelines"),
    )
    pipeline.run(source)
    return db_path


def _fixture_records(number_for_id_1: int = 101) -> list[dict]:
    """Two issues, each with a child array and an object holding a grandchild array."""
    return [
        {
            "id": 1,
            "number": number_for_id_1,
            "labels": [{"name": "bug"}, {"name": "docs"}],
            "performed_via_github_app": {"events": [{"name": "push"}]},
        },
        {
            "id": 2,
            "number": 202,
            "labels": [{"name": "wontfix"}],
            "performed_via_github_app": {"events": [{"name": "issues"}]},
        },
    ]


def test_child_and_grandchild_rows_carry_the_parent_key(tmp_path: Path) -> None:
    db_path = _run_fixture_pipeline(tmp_path, _fixture_records())

    with duckdb.connect(str(db_path)) as conn:
        labels = conn.execute(
            "SELECT issue_number, name FROM github.issues__labels ORDER BY issue_number, name"
        ).fetchall()
        events = conn.execute(
            "SELECT issue_number, name FROM "
            "github.issues__performed_via_github_app__events ORDER BY issue_number"
        ).fetchall()

    assert labels == [(101, "bug"), (101, "docs"), (202, "wontfix")]
    assert events == [(101, "push"), (202, "issues")]


def test_merging_an_edited_parent_leaves_no_stale_child_keys(tmp_path: Path) -> None:
    """A merge deletes and re-inserts a root's children, so the propagated value
    follows an edit rather than sticking. Pinned because a dlt upgrade could break it
    silently: the stale rows would still join, to the wrong parent."""
    _run_fixture_pipeline(tmp_path, _fixture_records())
    db_path = _run_fixture_pipeline(tmp_path, _fixture_records(number_for_id_1=999))

    with duckdb.connect(str(db_path)) as conn:
        labels = conn.execute(
            "SELECT issue_number, name FROM github.issues__labels ORDER BY issue_number, name"
        ).fetchall()

    assert labels == [(202, "wontfix"), (999, "bug"), (999, "docs")]

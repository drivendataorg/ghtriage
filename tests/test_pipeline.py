from pathlib import Path
from unittest.mock import Mock

import duckdb
import pytest

from ghtriage.pipeline import _write_meta, run_pull


def _install_pipeline_mocks(monkeypatch):
    sentinel_destination = object()
    sentinel_source = object()
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
    mock_create_derived = Mock(side_effect=lambda *_a, **_k: call_order.append("create_derived"))
    monkeypatch.setattr("ghtriage.pipeline.create_derived", mock_create_derived)
    mock_create_search_indexes = Mock(
        side_effect=lambda *_a, **_k: call_order.append("create_search_indexes")
    )
    monkeypatch.setattr("ghtriage.pipeline.create_search_indexes", mock_create_search_indexes)
    mock_fetch_and_annotate = Mock(
        side_effect=lambda *_a, **_k: call_order.append("fetch_and_annotate")
    )
    monkeypatch.setattr("ghtriage.pipeline.fetch_and_annotate", mock_fetch_and_annotate)

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
    assert call_order == ["create_derived", "create_search_indexes", "fetch_and_annotate"]


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
    assert call_order == ["create_derived", "create_search_indexes", "fetch_and_annotate"]


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
    assert call_order == ["create_derived", "create_search_indexes", "fetch_and_annotate"]


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


def test_run_pull_drops_derived_tables_when_the_derive_step_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """Left in place, they would be indexed as though they were current."""
    _install_pipeline_mocks(monkeypatch)
    monkeypatch.setattr("ghtriage.pipeline.create_derived", Mock(side_effect=RuntimeError("boom")))
    mock_drop = Mock()
    monkeypatch.setattr("ghtriage.pipeline.drop_derived_tables", mock_drop)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=False)

    mock_drop.assert_called_once_with(tmp_path / ".ghtriage" / "ghtriage.duckdb")


def test_run_pull_does_not_drop_derived_tables_when_the_derive_step_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    _install_pipeline_mocks(monkeypatch)
    mock_drop = Mock()
    monkeypatch.setattr("ghtriage.pipeline.drop_derived_tables", mock_drop)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=False)

    mock_drop.assert_not_called()

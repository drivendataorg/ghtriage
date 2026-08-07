from pathlib import Path
from unittest.mock import Mock

import duckdb

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
    mock_create_views = Mock(side_effect=lambda *_a, **_k: call_order.append("create_views"))
    monkeypatch.setattr("ghtriage.pipeline.create_views", mock_create_views)
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
        mock_create_views,
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
        mock_create_views,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    load_info, meta_error = run_pull(repo="owner/repo", token="tok", full=False, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert meta_error is None
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
    assert resource_names == ["issues", "pulls", "issue_comments", "pull_comments"]

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
        _mock_create_views,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    ghtriage_dir = tmp_path / ".ghtriage"
    pipelines_dir = ghtriage_dir / "pipelines"
    old_pipeline_file = pipelines_dir / "stale" / "marker.txt"
    old_db_path = ghtriage_dir / "ghtriage.duckdb"

    old_pipeline_file.parent.mkdir(parents=True, exist_ok=True)
    old_pipeline_file.write_text("old", encoding="utf-8")
    old_db_path.write_text("old", encoding="utf-8")

    load_info, meta_error = run_pull(repo="owner/repo", token="tok", full=True, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert meta_error is None
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
        _mock_create_views,
        _call_order,
    ) = _install_pipeline_mocks(monkeypatch)

    load_info, meta_error = run_pull(repo="owner/repo", token="tok", full=True, cwd=tmp_path)

    assert load_info is sentinel_run_result
    assert meta_error is None
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
        _mock_create_views,
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


def test_run_pull_creates_views_before_annotating(tmp_path: Path, monkeypatch) -> None:
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
        mock_create_views,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=False)

    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    mock_create_views.assert_called_once_with(db_path)
    assert call_order == ["create_views", "fetch_and_annotate"]


def test_run_pull_creates_views_on_full_rebuild(tmp_path: Path, monkeypatch) -> None:
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
        mock_create_views,
        call_order,
    ) = _install_pipeline_mocks(monkeypatch)
    monkeypatch.chdir(tmp_path)

    run_pull(repo="owner/repo", token="t", full=True)

    mock_create_views.assert_called_once()
    assert call_order == ["create_views", "fetch_and_annotate"]

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

import dlt
from dlt.sources.rest_api import rest_api_source
import duckdb

from ghtriage.annotations import fetch_and_annotate
from ghtriage.config import get_db_path, get_pipelines_dir
from ghtriage.full_text_search import create_search_indexes
from ghtriage.views import create_views


def _split_repo(repo: str) -> tuple[str, str]:
    owner, name = repo.split("/", 1)
    return owner, name


def _is_issue(item: Any) -> bool:
    return isinstance(item, dict) and item.get("pull_request") is None


def build_rest_api_source(repo: str, token: str):
    owner, name = _split_repo(repo)
    base_url = f"https://api.github.com/repos/{owner}/{name}/"

    source_config = {
        "client": {
            "base_url": base_url,
            "auth": {"token": token},
            "headers": {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            "paginator": "header_link",
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                }
            },
        },
        "resources": [
            {
                "name": "issues",
                "processing_steps": [{"filter": _is_issue}],
                "endpoint": {
                    "path": "issues",
                    "params": {
                        "state": "all",
                        "sort": "updated",
                        "direction": "desc",
                    },
                    "incremental": {
                        "cursor_path": "updated_at",
                        "start_param": "since",
                    },
                },
            },
            {
                "name": "pull_requests",
                "endpoint": {
                    "path": "pulls",
                    "params": {
                        "state": "all",
                        "sort": "updated",
                        "direction": "desc",
                    },
                    "incremental": {
                        "cursor_path": "updated_at",
                    },
                },
            },
            {
                "name": "conversation_comments",
                "endpoint": {
                    "path": "issues/comments",
                    "params": {
                        "sort": "updated",
                        "direction": "desc",
                    },
                    "incremental": {
                        "cursor_path": "updated_at",
                        "start_param": "since",
                    },
                },
            },
            {
                "name": "review_comments",
                "endpoint": {
                    "path": "pulls/comments",
                    "params": {
                        "sort": "updated",
                        "direction": "desc",
                    },
                    "incremental": {
                        "cursor_path": "updated_at",
                        "start_param": "since",
                    },
                },
            },
        ],
    }
    return rest_api_source(source_config)


def _write_meta(db_path: Path, repo: str, full: bool) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with duckdb.connect(str(db_path)) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS github")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS github._ghtriage_meta (
                key   VARCHAR PRIMARY KEY,
                value VARCHAR
            )
        """)
        for key, value in [
            ("repo", repo),
            ("last_pull_at", now),
            ("last_full_pull", str(full).lower()),
        ]:
            conn.execute(
                """
                INSERT INTO github._ghtriage_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                [key, value],
            )


def create_pipeline(cwd: str | Path | None = None):
    db_path = get_db_path(cwd=cwd)
    pipelines_dir = get_pipelines_dir(cwd=cwd)
    pipelines_dir.mkdir(parents=True, exist_ok=True)

    return dlt.pipeline(
        pipeline_name="ghtriage",
        destination=dlt.destinations.duckdb(str(db_path)),
        dataset_name="github",
        pipelines_dir=str(pipelines_dir),
    )


def run_pull(
    repo: str,
    token: str,
    *,
    full: bool = False,
    cwd: str | Path | None = None,
):
    db_path = get_db_path(cwd=cwd)
    pipelines_dir = get_pipelines_dir(cwd=cwd)

    if full:
        if db_path.exists():
            db_path.unlink()
        if pipelines_dir.exists():
            shutil.rmtree(pipelines_dir)

    pipeline = create_pipeline(cwd=cwd)
    source = build_rest_api_source(repo=repo, token=token)
    load_info = pipeline.run(source)
    meta_error: Exception | None = None
    try:
        _write_meta(db_path=db_path, repo=repo, full=full)
    except Exception as exc:
        meta_error = exc
    create_views(db_path)
    create_search_indexes(db_path)
    fetch_and_annotate(db_path)
    return load_info, meta_error

from datetime import datetime, timezone
from pathlib import Path
import shutil
from typing import Any

import dlt
from dlt.common.normalizers.json.relational import DataItemNormalizer
from dlt.sources.rest_api import rest_api_source
import duckdb

from ghtriage.annotations import fetch_and_annotate
from ghtriage.config import get_db_path, get_pipelines_dir
from ghtriage.derived import create_derived
from ghtriage.full_text_search import create_search_indexes

# The GitHub key each child table carries from its parent, so `issues__labels` joins on
# `issue_number` rather than on the loader's own row link. dlt applies this recursively,
# so any child table it creates under these parents gets the key too. Comments have no
# number, so `id` is their key. `review_comments` has no arrays and so propagates
# nothing today; it is listed so that a child table dlt creates there later is not the
# one without a key.
KEY_PROPAGATION = {
    "issues": {"number": "issue_number"},
    "pull_requests": {"number": "pull_request_number"},
    "conversation_comments": {"id": "comment_id"},
    "review_comments": {"id": "review_comment_id"},
}

# Bump when a change alters the shape of the raw tables an existing database already
# holds (propagation config, resources, primary keys, table renames). Derived views and
# full-text indexes never require a bump: they are rebuilt from scratch on every pull
# and cannot go stale.
SCHEMA_GENERATION = 1


class SchemaGenerationMismatch(RuntimeError):
    """An incremental pull was aimed at a database whose raw-table shape is not this one's."""

    def __init__(self, stored: int, current: int) -> None:
        super().__init__(
            "This database was created by a ghtriage version with a different schema "
            f"(generation {stored}; this version writes generation {current}).\n"
            "Run `ghtriage pull --full` to rebuild it."
        )


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
    source = rest_api_source(source_config)
    # Copied because dlt normalizes the identifiers in place.
    DataItemNormalizer.update_normalizer_config(
        source.schema, {"propagation": {"tables": dict(KEY_PROPAGATION)}}
    )
    return source


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
            ("schema_generation", str(SCHEMA_GENERATION)),
        ]:
            conn.execute(
                """
                INSERT INTO github._ghtriage_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                [key, value],
            )


def _read_schema_generation(db_path: Path) -> int:
    """Read the stamp an earlier pull left. Anything written before it existed reads as 0."""
    with duckdb.connect(str(db_path), read_only=True) as conn:
        try:
            row = conn.execute(
                "SELECT value FROM github._ghtriage_meta WHERE key = 'schema_generation'"
            ).fetchone()
        except duckdb.CatalogException:
            return 0
    return 0 if row is None else int(row[0])


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
    elif db_path.exists():
        # Before the load, so a refused pull leaves the database old but internally
        # consistent. There is no upgrade path other than `pull --full`, by design.
        stored = _read_schema_generation(db_path)
        if stored != SCHEMA_GENERATION:
            raise SchemaGenerationMismatch(stored=stored, current=SCHEMA_GENERATION)

    pipeline = create_pipeline(cwd=cwd)
    source = build_rest_api_source(repo=repo, token=token)
    load_info = pipeline.run(source)

    # The load is what a pull is for; everything below decorates it, and any of it
    # failing still leaves the raw tables usable. Each step is attempted, reported and
    # survived -- this is the only place that judgment is made.
    warnings: list[str] = []

    try:
        _write_meta(db_path=db_path, repo=repo, full=full)
    except Exception as exc:
        warnings.append(f"metadata write failed: {exc}")

    try:
        warnings.extend(create_derived(db_path))
    except Exception as exc:
        # Per-object failures already drop what they could not rebuild. A wholesale
        # failure -- the database cannot be opened or probed -- leaves the user one
        # `ghtriage pull --full` from clean, which the CLI says next to this warning.
        warnings.append(f"derived objects failed: {exc}")

    try:
        warnings.extend(create_search_indexes(db_path))
    except Exception as exc:
        warnings.append(f"full-text indexes failed: {exc}")

    try:
        fetch_and_annotate(db_path)
    except Exception as exc:
        warnings.append(f"schema annotation failed: {exc}")

    return load_info, warnings

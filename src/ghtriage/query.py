from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ghtriage.config import get_db_path
from ghtriage.full_text_search import INDEXES

_MAIN_TABLES = ("issues", "pull_requests", "conversation_comments", "review_comments")


def _resolve_db_path(cwd: str | Path | None = None) -> Path:
    db_path = get_db_path(cwd=cwd, create=False)
    if not db_path.exists():
        raise RuntimeError(
            f"Database not found at {db_path}. Run `ghtriage pull` to create it first."
        )
    return db_path


def execute_query(sql: str, cwd: str | Path | None = None) -> tuple[list[str], list[tuple]]:
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        conn.execute("SET schema = 'github'")
        cursor = conn.execute(sql)

        if cursor.description is None:
            return [], []

        columns = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return columns, rows


def get_tables(
    cwd: str | Path | None = None,
    *,
    include_internal: bool = False,
) -> list[str]:
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'github'
            ORDER BY table_name
            """
        ).fetchall()

    tables = [row[0] for row in rows]
    if include_internal:
        return tables
    return [table for table in tables if not table.startswith("_dlt_")]


def get_table_columns(
    table_name: str,
    cwd: str | Path | None = None,
) -> list[tuple[str, str, bool, str | None]]:
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT c.column_name, c.data_type, c.is_nullable, dc.comment
            FROM information_schema.columns c
            LEFT JOIN (
                SELECT column_name, comment
                FROM duckdb_columns()
                WHERE schema_name = 'github' AND table_name = ?
            ) dc ON dc.column_name = c.column_name
            WHERE c.table_schema = 'github' AND c.table_name = ?
            ORDER BY c.ordinal_position
            """,
            [table_name, table_name],
        ).fetchall()

    if not rows:
        raise ValueError(f"Table not found in github schema: {table_name}")

    return [
        (name, data_type, is_nullable == "YES", comment)
        for name, data_type, is_nullable, comment in rows
    ]


def get_table_descriptions(cwd: str | Path | None = None) -> dict[str, str]:
    """Return {name: description} for tables and views that have a comment set.

    duckdb_tables() excludes views, so derived views are unioned in explicitly.
    """
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT table_name, comment FROM duckdb_tables() WHERE schema_name = 'github'
            UNION ALL
            SELECT view_name, comment FROM duckdb_views() WHERE schema_name = 'github'
            """
        ).fetchall()
    return {name: comment for name, comment in rows if comment}


@dataclass
class FullTextIndex:
    table: str
    key_column: str | None
    columns: list[str]
    document_count: int


def get_full_text_indexes(cwd: str | Path | None = None) -> list[FullTextIndex]:
    """Return the full-text indexes present in the database, ordered by table name.

    Existence, indexed columns and document counts are read from each index's own
    catalog tables, so they cannot drift from what was actually built. The key column
    is not recoverable that way -- `docs` stores key values under an opaque `name` --
    so it comes from the declaration, and is None for an index ghtriage did not declare.
    """
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        schemas = [
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM duckdb_schemas() "
                "WHERE schema_name LIKE 'fts_github_%' ORDER BY schema_name"
            ).fetchall()
        ]

        indexes = []
        for schema in schemas:
            table = schema.removeprefix("fts_github_")
            columns = [
                row[0]
                for row in conn.execute(
                    f'SELECT field FROM "{schema}".fields ORDER BY fieldid'  # noqa: S608
                ).fetchall()
            ]
            count = conn.execute(
                f'SELECT count(*) FROM "{schema}".docs'  # noqa: S608
            ).fetchone()[0]
            declared = INDEXES.get(table)
            indexes.append(
                FullTextIndex(
                    table=table,
                    key_column=declared[0] if declared else None,
                    columns=columns,
                    document_count=count,
                )
            )
    return indexes


@dataclass
class StatusData:
    db_path: Path
    db_size_bytes: int
    db_repo: str | None
    last_pull_at: str | None
    last_full_pull: bool | None
    table_stats: list[tuple[str, int, str | None]] = field(default_factory=list)


def get_status_data(cwd: str | Path | None = None) -> StatusData:
    db_path = _resolve_db_path(cwd=cwd)
    db_size_bytes = db_path.stat().st_size

    with duckdb.connect(str(db_path), read_only=True) as conn:
        db_repo = None
        last_pull_at = None
        last_full_pull = None
        try:
            rows = conn.execute("SELECT key, value FROM github._ghtriage_meta").fetchall()
            meta = dict(rows)
            db_repo = meta.get("repo")
            last_pull_at = meta.get("last_pull_at")
            if (raw := meta.get("last_full_pull")) is not None:
                last_full_pull = raw == "true"
        except duckdb.CatalogException:
            pass

        table_stats = []
        for table in _MAIN_TABLES:
            try:
                row = conn.execute(
                    f"SELECT COUNT(*), MAX(updated_at) FROM github.{table}"  # noqa: S608
                ).fetchone()
                count = row[0] or 0
                max_updated_at = str(row[1])[:19] if row[1] is not None else None
                table_stats.append((table, count, max_updated_at))
            except Exception:
                pass

    return StatusData(
        db_path=db_path,
        db_size_bytes=db_size_bytes,
        db_repo=db_repo,
        last_pull_at=last_pull_at,
        last_full_pull=last_full_pull,
        table_stats=table_stats,
    )

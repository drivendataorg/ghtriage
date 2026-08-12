from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ghtriage.config import get_db_path
from ghtriage.full_text_search import INDEXES, index_schema

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


def get_tables(cwd: str | Path | None = None) -> list[str]:
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

    return [row[0] for row in rows if not row[0].startswith("_dlt_")]


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

    # The loader's row links are how the normalizer builds child tables, not part of the
    # documented surface; the propagated GitHub keys are the join. Still selectable by
    # name, and `information_schema` through `ghtriage query` remains the ground truth.
    return [
        (name, data_type, is_nullable == "YES", comment)
        for name, data_type, is_nullable, comment in rows
        if not name.startswith("_dlt_")
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
    key_column: str
    columns: list[str]
    document_count: int


def get_full_text_indexes(cwd: str | Path | None = None) -> list[FullTextIndex]:
    """Return the declared full-text indexes that exist, in declaration order.

    The declaration is authoritative: an index that cannot cover exactly its declared
    key and columns is dropped rather than built, so there is nothing to introspect.
    The document count comes from the indexed table, which equals the index's own count
    because the two are rebuilt in the same pull and nothing writes between pulls. An
    `fts_github_%` schema ghtriage did not declare is not ours to describe, so it is
    left out rather than half-reported.
    """
    db_path = _resolve_db_path(cwd=cwd)
    with duckdb.connect(str(db_path), read_only=True) as conn:
        present = {
            row[0]
            for row in conn.execute(
                "SELECT schema_name FROM duckdb_schemas() WHERE schema_name LIKE 'fts_github_%'"
            ).fetchall()
        }
        return [
            FullTextIndex(
                table=table,
                key_column=key_column,
                columns=list(columns),
                document_count=conn.execute(f"SELECT count(*) FROM github.{table}").fetchone()[0],
            )
            for table, (key_column, columns) in INDEXES.items()
            if index_schema(table) in present
        ]


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
                    f"SELECT COUNT(*), MAX(updated_at) FROM github.{table}"
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

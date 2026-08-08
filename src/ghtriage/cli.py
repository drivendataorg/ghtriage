import argparse
import csv
import json
from pathlib import Path
import sys
from typing import Sequence

from ghtriage.config import get_db_path, resolve_repo, resolve_token
from ghtriage.full_text_search import INDEXES
from ghtriage.pipeline import run_pull
from ghtriage.query import (
    FullTextIndex,
    execute_query,
    get_full_text_indexes,
    get_status_data,
    get_table_columns,
    get_table_descriptions,
    get_tables,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghtriage",
        description="Query this repo's GitHub issues, PRs, and comments using SQL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pull_parser = subparsers.add_parser("pull", help="Pull GitHub data into local DuckDB")
    pull_parser.add_argument("--repo", help="GitHub repository in OWNER/REPO format")
    pull_parser.add_argument(
        "--full",
        action="store_true",
        help="Delete local DB and pipeline state before pulling",
    )

    query_parser = subparsers.add_parser("query", help="Run SQL against local DuckDB")
    query_parser.add_argument("sql", help="SQL statement")
    query_parser.add_argument(
        "--format",
        choices=("table", "csv", "json"),
        default="table",
        help="Output format",
    )

    schema_parser = subparsers.add_parser("schema", help="Inspect schema")
    schema_parser.add_argument("--table", help="Table name")

    subparsers.add_parser("status", help="Show database state and data summary")

    return parser


def _run_pull(args: argparse.Namespace) -> int:
    repo = resolve_repo(cli_repo=args.repo)
    token, _ = resolve_token()
    if token is None:
        print(
            "Missing GitHub token. Set GITHUB_TOKEN or place a token in .ghtriage/token.",
            file=sys.stderr,
        )
        return 1
    load_info, meta_error = run_pull(repo=repo, token=token, full=args.full)
    print(f"Pull completed for {repo}")
    print(load_info)
    if meta_error is not None:
        print(f"Warning: metadata write failed: {meta_error}", file=sys.stderr)
    return 0


def _format_table(columns: list[str], rows: list[tuple]) -> None:
    if not columns:
        return

    string_rows = [[str(value) for value in row] for row in rows]
    widths = [len(column) for column in columns]
    for row in string_rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    header = " | ".join(column.ljust(widths[index]) for index, column in enumerate(columns))
    separator = "-+-".join("-" * width for width in widths)
    print(header)
    print(separator)

    for row in string_rows:
        print(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _format_csv(columns: list[str], rows: list[tuple]) -> None:
    if not columns:
        return
    writer = csv.writer(sys.stdout, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)


def _format_jsonl(columns: list[str], rows: list[tuple]) -> None:
    if not columns:
        return
    for row in rows:
        record = dict(zip(columns, row, strict=True))
        print(json.dumps(record, default=str))


def _run_query(args: argparse.Namespace) -> int:
    try:
        columns, rows = execute_query(args.sql)
    except Exception as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1

    if args.format == "table":
        _format_table(columns, rows)
        return 0
    if args.format == "csv":
        _format_csv(columns, rows)
        return 0
    if args.format == "json":
        _format_jsonl(columns, rows)
        return 0

    print(f"Unsupported format: {args.format}", file=sys.stderr)
    return 1


def _print_index_block(indexes: list[FullTextIndex]) -> None:
    """The searchable surface, read from the indexes themselves.

    An agent that cannot discover an index may as well not have one, and the
    fail-closed note is here because nothing else can report it: an id scored against
    another table's index simply returns nothing.
    """
    if not indexes:
        return

    print()
    print("Full-text search indexes")
    print()
    _format_table(
        ["table", "key", "indexed columns", "documents"],
        [
            (
                index.table,
                index.key_column or "",
                ", ".join(index.columns),
                f"{index.document_count:,}",
            )
            for index in indexes
        ],
    )
    # Listed alphabetically, but demonstrated with whichever index comes first in the
    # declaration -- `issues` reads as an example in a way that a comment table does not.
    order = list(INDEXES)
    example = min(
        indexes,
        key=lambda index: order.index(index.table) if index.table in order else len(order),
    )
    key = example.key_column or "id"
    print()
    print("Search a table by scoring its key column against its own index:")
    print("  SELECT *, score FROM (")
    print(f"      SELECT *, fts_github_{example.table}.match_bm25({key}, 'search terms') AS score")
    print(f"      FROM {example.table}")
    print("  ) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10")
    print()
    print("The macro is keyed on the document id, not bound to its table, so it also works")
    print("from any view carrying that id. An id the index does not hold scores NULL, so")
    print("pairing a table with another table's index returns no rows rather than an error.")


def _run_schema(args: argparse.Namespace) -> int:
    try:
        if args.table:
            indexes = [i for i in get_full_text_indexes() if i.table == args.table]
            if indexes:
                index = indexes[0]
                print(
                    f"Full-text search: fts_github_{index.table}."
                    f"match_bm25({index.key_column or 'id'}, 'query') "
                    f"over {', '.join(index.columns)} ({index.document_count:,} documents)"
                )
                print()
            columns = get_table_columns(args.table)
            has_descriptions = any(desc is not None for _, _, _, desc in columns)
            if has_descriptions:
                _format_table(
                    ["column_name", "data_type", "nullable", "description"],
                    [
                        (name, dtype, str(nullable), desc or "")
                        for name, dtype, nullable, desc in columns
                    ],
                )
            else:
                _format_table(
                    ["column_name", "data_type", "nullable"],
                    [(name, dtype, str(nullable)) for name, dtype, nullable, _ in columns],
                )
            return 0

        tables = get_tables()
        descriptions = get_table_descriptions()
        if any(t in descriptions for t in tables):
            _format_table(
                ["table", "description"],
                [(t, descriptions.get(t, "")) for t in tables],
            )
        else:
            for table in tables:
                print(table)
        _print_index_block(get_full_text_indexes())
        return 0
    except Exception as exc:
        print(f"Schema inspection failed: {exc}", file=sys.stderr)
        return 1


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_pull_at(iso_str: str) -> str:
    return iso_str.replace("T", " ").replace("Z", " UTC")


def _run_status(args: argparse.Namespace) -> int:
    try:
        config_repo: str | None = resolve_repo()
    except Exception:
        config_repo = None

    _, token_source = resolve_token()

    db_path = get_db_path(create=False)
    try:
        display_db_path = db_path.relative_to(Path.cwd())
    except ValueError:
        display_db_path = db_path

    print(f"Config repo:  {config_repo or 'unknown'}")
    print(f"Token:        {token_source}")

    if not db_path.exists():
        print(f"Database:     {display_db_path} (not yet pulled)")
        return 0

    try:
        status = get_status_data()
    except Exception as exc:
        print(f"Database:     {display_db_path}", file=sys.stderr)
        print(f"Error reading status: {exc}", file=sys.stderr)
        return 1

    print(f"DB repo:      {status.db_repo or 'unknown'}")
    print(f"Database:     {display_db_path} ({_format_size(status.db_size_bytes)})")
    print(
        f"Last pull:    "
        f"{_format_pull_at(status.last_pull_at) if status.last_pull_at else 'unknown'}"
    )

    if config_repo and status.db_repo and config_repo != status.db_repo:
        print()
        print(f"WARNING: Config repo does not match DB repo. Next pull will target {config_repo}.")
        print(f"         Run `ghtriage pull --full` to rebuild for {config_repo}.")

    if status.table_stats:
        print()
        _format_table(
            ["Table", "Rows", "Latest updated_at"],
            [(name, f"{count:,}", max_upd or "—") for name, count, max_upd in status.table_stats],
        )

    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "pull":
        return _run_pull(args)
    if args.command == "query":
        return _run_query(args)
    if args.command == "schema":
        return _run_schema(args)
    if args.command == "status":
        return _run_status(args)

    parser.error(f"Unknown command: {args.command}")

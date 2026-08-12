import json
from pathlib import Path
import urllib.error
import urllib.request

import duckdb

OPENAPI_SPEC_URL = "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json"
OPENAPI_FETCH_TIMEOUT_SECONDS = 30

# Maps DuckDB table name → OpenAPI component schema name
TABLE_SCHEMAS = {
    "issues": "issue",
    "pull_requests": "pull-request-simple",
    "conversation_comments": "issue-comment",
    "review_comments": "pull-request-review-comment",
}


# Maps parent table → (propagated key column, description). dlt puts the column on every
# child table under that parent (see KEY_PROPAGATION in pipeline.py), including ones it
# creates later, so the description is applied by parent rather than listed per table.
# These columns are ghtriage's own addition and are absent from GitHub's OpenAPI
# schemas, which is why their text is written here rather than fetched.
PROPAGATED_KEY_DESCRIPTIONS = {
    "issues": (
        "issue_number",
        "Propagated from the parent issue; join to `issues.number`.",
    ),
    "pull_requests": (
        "pull_request_number",
        "Propagated from the parent pull request; join to `pull_requests.number`.",
    ),
    "conversation_comments": (
        "comment_id",
        "Propagated from the parent comment; join to `conversation_comments.id`.",
    ),
    "review_comments": (
        "review_comment_id",
        "Propagated from the parent review comment; join to `review_comments.id`.",
    ),
}


def annotate_propagated_keys(db_path: Path) -> None:
    """
    Apply COMMENT ON COLUMN to the GitHub keys child tables carry from their parent.

    Deliberately not part of fetch_and_annotate: these descriptions are written here, so
    they have no reason to depend on a network fetch succeeding.
    Tables or columns absent from the database are silently skipped.
    """
    with duckdb.connect(str(db_path)) as conn:
        existing = conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'github'"
        ).fetchall()

        for parent, (column, desc) in PROPAGATED_KEY_DESCRIPTIONS.items():
            for table, existing_column in existing:
                if existing_column == column and table.startswith(f"{parent}__"):
                    conn.execute(f"COMMENT ON COLUMN github.{table}.{column} IS '{desc}'")


def fetch_spec(url: str, timeout_seconds: int = OPENAPI_FETCH_TIMEOUT_SECONDS) -> dict:
    """Download and parse an OpenAPI spec from a URL."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Failed to fetch OpenAPI spec: HTTP {exc.code} {exc.reason}") from exc
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch OpenAPI spec: {exc}") from exc


def _resolve_ref(schema: dict, spec: dict) -> dict:
    """Resolve a $ref pointer to its target schema. Returns schema unchanged if no $ref."""
    if "$ref" not in schema:
        return schema
    # Format: "#/components/schemas/Foo"
    ref_path = schema["$ref"].lstrip("#/").split("/")
    result = spec
    for part in ref_path:
        result = result[part]
    return result


def _extract_descriptions(schema: dict, spec: dict, prefix: str = "") -> dict[str, str]:
    """
    Recursively walk schema properties and collect {flattened_column_name: description}.

    Nested object properties are flattened using '__' as separator, matching dlt's convention.
    Array-type properties are skipped — dlt stores them in child tables, not columns.
    _resolve_ref is called at the start of each invocation so multi-level $ref chains
    are handled correctly at every depth.
    """
    schema = _resolve_ref(schema, spec)
    result = {}

    for prop_name, prop_schema in schema.get("properties", {}).items():
        column_key = f"{prefix}__{prop_name}" if prefix else prop_name

        if "description" in prop_schema:
            result[column_key] = prop_schema["description"]

        # Resolve ref to determine type and whether to recurse
        resolved = _resolve_ref(prop_schema, spec)

        # Skip arrays — they become child tables in dlt
        if resolved.get("type") == "array" or prop_schema.get("type") == "array":
            continue

        # Recurse into inline objects and $ref objects that have properties
        if "properties" in resolved:
            result.update(_extract_descriptions(resolved, spec, prefix=column_key))

    return result


def build_column_descriptions(spec: dict) -> dict[str, dict[str, str]]:
    """Return {table_name: {column_name: description}} for all tracked tables."""
    result = {}
    for table_name, schema_name in TABLE_SCHEMAS.items():
        schema = spec["components"]["schemas"][schema_name]
        result[table_name] = _extract_descriptions(schema, spec)
    return result


def build_table_descriptions(spec: dict) -> dict[str, str]:
    """Return {table_name: description} using the schema-level description for each table."""
    result = {}
    for table_name, schema_name in TABLE_SCHEMAS.items():
        schema = spec["components"]["schemas"][schema_name]
        if desc := schema.get("description"):
            result[table_name] = desc
    return result


def annotate_database(
    db_path: Path,
    table_descs: dict[str, str],
    column_descs: dict[str, dict[str, str]],
) -> None:
    """
    Apply COMMENT ON TABLE and COMMENT ON COLUMN for all known descriptions.

    Tables or columns absent from the database are silently skipped.
    Single quotes in descriptions are escaped as '' (DDL does not support bound parameters).
    """
    with duckdb.connect(str(db_path)) as conn:
        existing_tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'github'"
            ).fetchall()
        }

        for table, desc in table_descs.items():
            if table not in existing_tables:
                continue
            escaped = desc.replace("'", "''")
            conn.execute(f"COMMENT ON TABLE github.{table} IS '{escaped}'")

        for table, col_descriptions in column_descs.items():
            if table not in existing_tables:
                continue
            existing_cols = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'github' AND table_name = ?",
                    [table],
                ).fetchall()
            }
            for column, desc in col_descriptions.items():
                if column not in existing_cols:
                    continue
                escaped = desc.replace("'", "''")
                conn.execute(f"COMMENT ON COLUMN github.{table}.{column} IS '{escaped}'")


def fetch_and_annotate(db_path: Path) -> None:
    """
    Fetch the GitHub OpenAPI spec and annotate the database with field descriptions.

    Raises if the spec cannot be fetched or applied.
    """
    spec = fetch_spec(OPENAPI_SPEC_URL)
    table_descs = build_table_descriptions(spec)
    column_descs = build_column_descriptions(spec)
    annotate_database(db_path, table_descs, column_descs)

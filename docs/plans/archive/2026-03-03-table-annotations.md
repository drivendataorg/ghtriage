# Plan: Annotate DuckDB columns with GitHub OpenAPI descriptions at pull time

## Context

`ghtriage` is a CLI that an agent runs in *another* repository. After `ghtriage pull`, the agent uses `ghtriage schema --table <name>` to understand the data before writing queries. Currently that command shows column names and types only — no descriptions. This makes it hard for an agent to know what columns like `state_reason`, `performed_via_github_app__slug`, or `reactions__plus_one` mean.

The GitHub REST API has a public OpenAPI spec (`github/rest-api-description`) that includes field-level descriptions for some properties and schema-level descriptions for all 4 resource types. Fetching this spec at pull time and storing descriptions as DuckDB column comments means `ghtriage schema --table issues` will show meaningful documentation with zero extra steps for the agent.

**Coverage note**: The spec descriptions are sparse on scalar fields for `issues` (8/40) and `pulls` (1/36), richer for `pull_comments` (21/29), and moderate for `issue_comments` (3/15). Schema-level descriptions exist for all 4 resources. This is whatever GitHub officially documents and will improve automatically as GitHub updates their spec.

## Approach

### 1. New module: `src/ghtriage/annotations.py`

Responsible for fetching the spec, extracting descriptions, and applying them to the database.

**Constants:**
```python
OPENAPI_SPEC_URL = "https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json"

# Maps DuckDB table name → OpenAPI schema name
TABLE_SCHEMAS = {
    "issues": "issue",
    "pulls": "pull-request-simple",
    "issue_comments": "issue-comment",
    "pull_comments": "pull-request-review-comment",
}
```

**`fetch_spec(url: str) -> dict`**
- Uses `urllib.request` (stdlib, no new dependency)
- Returns parsed JSON dict
- Raises `RuntimeError` on HTTP or parse failure

**`_resolve_ref(schema: dict, spec: dict) -> dict`**
- Resolves `$ref: "#/components/schemas/Foo"` to the actual schema dict
- Returns schema unchanged if no `$ref`

**`_extract_descriptions(schema: dict, spec: dict, prefix: str = "") -> dict[str, str]`**
- Calls `_resolve_ref` at the start of each invocation, not just the top level — multi-level `$ref` chains are handled correctly at every depth
- Recursively walks properties
- For each property with a `description`, adds `{flattened_path: description}`
- Flattened path: nested property names joined with `__` (e.g. `user.login` → `user__login`)
- Does NOT recurse into array-type properties (arrays become child tables in dlt, not columns)
- Does recurse into object-type properties and resolved `$ref` objects

**`build_column_descriptions(spec: dict) -> dict[str, dict[str, str]]`**
- Returns `{table_name: {column_name: description}}`
- Calls `_extract_descriptions` for each of the 4 TABLE_SCHEMAS entries

**`build_table_descriptions(spec: dict) -> dict[str, str]`**
- Returns `{table_name: description}` using the schema-level `description` field from each entry in TABLE_SCHEMAS
- e.g. `"issues"` → `"Issues are a great way to keep track of tasks, enhancements, and bugs for your project."`

**`annotate_database(db_path: Path, table_descs: dict[str, str], column_descs: dict[str, dict[str, str]]) -> None`**
- Opens DuckDB connection
- For each table, runs `COMMENT ON TABLE github.{table} IS '{desc}'` using `table_descs`
- For each table and column, runs `COMMENT ON COLUMN github.{table}.{column} IS '{desc}'` using `column_descs`
- Skips tables not in the descriptions dicts or not present in the database
- Escapes single quotes in description text using `.replace("'", "''")` — DDL statements do not support bound parameters

**`fetch_and_annotate(db_path: Path) -> None`**
- Top-level function called from `run_pull()`
- Calls `fetch_spec` → `build_table_descriptions` + `build_column_descriptions` → `annotate_database`
- Wraps everything in try/except — annotation failure prints a warning to stderr but does not fail the pull

### 2. Modify `src/ghtriage/pipeline.py`

In `run_pull()`, after `pipeline.run(source)`:

```python
from ghtriage.annotations import fetch_and_annotate

result = pipeline.run(source)
fetch_and_annotate(db_path)   # best-effort, swallows errors
return result
```

### 3. Modify `src/ghtriage/query.py`

**`get_table_columns()`** currently returns `list[tuple[str, str, bool]]`.

Change to return `list[tuple[str, str, bool, str | None]]` — adding description as the 4th element.

Join `information_schema.columns` with `duckdb_columns()` to include comments:

```sql
SELECT
    c.column_name,
    c.data_type,
    c.is_nullable = 'YES',
    dc.comment
FROM information_schema.columns c
LEFT JOIN (
    SELECT column_name, comment
    FROM duckdb_columns()
    WHERE schema_name = 'github' AND table_name = ?
) dc ON dc.column_name = c.column_name
WHERE c.table_schema = 'github' AND c.table_name = ?
ORDER BY c.ordinal_position
```

### 4. Modify `src/ghtriage/cli.py`

**`schema --table` handler**: Switch from the existing hardcoded pipe-separated format to `_format_table` so column widths are calculated properly (descriptions can be long). Include the description column when any row has a non-None value; omit it when all are None.

**`schema` (no-args) handler**: After listing table names, show the table-level comment if one exists. Use `duckdb_tables()` or a `COMMENT ON TABLE` lookup to retrieve it.

### 5. Tests

**New `tests/test_annotations.py`:**
- `test_fetch_spec_success`: mock `urllib.request.urlopen`, verify JSON parsed and returned
- `test_fetch_spec_http_error`: mock raises `urllib.error.HTTPError`, verify `RuntimeError` raised
- `test_extract_descriptions_scalar`: simple schema with description → flat dict
- `test_extract_descriptions_nested_object`: nested object property flattened with `__`
- `test_extract_descriptions_array_not_recursed`: array-type property not expanded
- `test_extract_descriptions_ref_resolved`: `$ref` resolved before extracting
- `test_extract_descriptions_multi_level_ref`: nested `$ref` chain resolved at each recursive level
- `test_extract_descriptions_single_quotes_escaped`: description containing `'` is escaped as `''`
- `test_build_column_descriptions_structure`: returns correct table keys
- `test_build_table_descriptions_structure`: returns correct table keys and schema-level descriptions
- `test_annotate_database_applies_column_comments`: real temp DuckDB, verify `duckdb_columns()` shows comment
- `test_annotate_database_applies_table_comments`: real temp DuckDB, verify `duckdb_tables()` shows comment
- `test_annotate_database_skips_missing_table`: doesn't crash if table doesn't exist
- `test_fetch_and_annotate_swallows_errors`: `fetch_spec` raises, no exception propagated

**Modify `tests/test_pipeline.py`:**
- Mock `ghtriage.pipeline.fetch_and_annotate`
- Verify it is called once after `pipeline.run()` in both full and non-full cases

**Modify `tests/test_query.py`:**
- Update `get_table_columns` tests to expect 4-tuples
- Add test: column with comment returns description in 4th element
- Add test: column without comment returns `None` in 4th element

**Modify `tests/test_cli.py`:**
- Add test: `schema --table` shows description column (uses `_format_table`) when comments are present
- Verify existing test: `schema --table` without comments still shows 3-column output (no description column)
- Add test: `schema` (no-args) shows table descriptions when table comments are present

## Files changed

| File | Change |
|------|--------|
| `src/ghtriage/annotations.py` | New |
| `src/ghtriage/pipeline.py` | Add `fetch_and_annotate` call after `pipeline.run()` |
| `src/ghtriage/query.py` | Add comment to `get_table_columns` return via `duckdb_columns()` join |
| `src/ghtriage/cli.py` | Use `_format_table` in `schema --table`; show description column when present; show table descriptions in `schema` listing |
| `tests/test_annotations.py` | New |
| `tests/test_pipeline.py` | Mock `fetch_and_annotate` |
| `tests/test_query.py` | Update for 4-tuple return type |
| `tests/test_cli.py` | Add/update schema description tests |

## Verification

```bash
# Run full test suite
uv run pytest

# Manual end-to-end (requires GITHUB_TOKEN)
ghtriage pull --full
ghtriage schema --table issues         # should show description column
ghtriage schema --table pull_comments  # pull_comments has the richest descriptions (21/29 fields)
```

# ghtriage: GitHub Triage Data Warehouse

## Context

We need a CLI tool that pulls GitHub issue, PR, and comment data into a local DuckDB database so a coding agent can query it for triage and project management. The tool lives in an existing skeleton repo with `pyproject.toml` (uv_build, Python ≥3.13) and a placeholder `src/ghtriage/__init__.py`.

## Approach

Use **dlt's built-in REST API source** (`dlt.sources.rest_api.rest_api_source`) with a declarative config dict targeting GitHub's REST API. This ships with the `dlt` pip package — no vendoring. dlt handles pagination, auth, incremental loading, and schema management. Their own docstring uses GitHub as the canonical example (see lines 157-200 of `dlt/sources/rest_api/__init__.py`).

Single dependency: `dlt[duckdb]>=1.0,<2`

## CLI Interface

```
ghtriage pull [--repo OWNER/REPO] [--full]
ghtriage query "SQL statement" [--format table|csv|json]
ghtriage schema [--table TABLE_NAME]
```

- `pull`: Fetch data from GitHub into local DuckDB. Auto-detects repo from git remote; `--repo` overrides. `--full` deletes the DuckDB file and pipeline state for a complete rebuild.
- `query`: Execute raw SQL. Default format is `table`; also supports `csv` and `json` (JSONL). Sets DuckDB schema to `github` so unqualified table names work.
- `schema`: No args lists tables; `--table X` shows columns and types.

## Storage Layout

```
.ghtriage/
├── config.toml          # Committable config: [repo] default, etc.
├── token                # Gitignored: raw GitHub token value
├── ghtriage.duckdb      # DuckDB database
└── pipelines/           # dlt pipeline state (incremental cursors)
```

Secrets (`token`, `*.duckdb`) are separated from config (`config.toml`) so that config can be committed while secrets stay gitignored.

## Auth Resolution Order

1. `GITHUB_TOKEN` env var (standard, works with `gh auth token`)
2. `.ghtriage/token` file containing a raw token string
3. Error with instructions if neither found

## Module Structure

```
src/ghtriage/
├── __init__.py      # main() entrypoint → calls cli.run()
├── cli.py           # argparse subcommands, output formatting, dispatch
├── config.py        # Token resolution, git remote parsing, .ghtriage/ dir mgmt
├── pipeline.py      # dlt REST API source config, pipeline creation, run_pull()
└── query.py         # DuckDB query execution, schema introspection
```

## Repo Resolution Order

1. `--repo OWNER/REPO` CLI flag (highest priority)
2. `.ghtriage/config.toml` → `[repo] default` value
3. Auto-detect from git remote origin URL

## Data Pipeline (`pipeline.py`)

Declarative REST API source config covering four GitHub endpoints:

| Resource | Endpoint | Incremental | Notes |
|----------|----------|-------------|-------|
| `issues` | `GET /repos/{o}/{r}/issues?state=all&sort=updated` | `updated_at` cursor, `since` param | Filtered: excludes rows where `pull_request` is not null |
| `pulls` | `GET /repos/{o}/{r}/pulls?state=all&sort=updated` | `updated_at` cursor (client-side only — no `since` param available) | Fetches sorted by updated desc; less efficient for incremental |
| `issue_comments` | `GET /repos/{o}/{r}/issues/comments?sort=updated` | `updated_at` cursor, `since` param | — |
| `pull_comments` | `GET /repos/{o}/{r}/pulls/comments?sort=updated` | `updated_at` cursor, `since` param | — |

**Issue/PR separation**: GitHub's `/issues` endpoint returns both issues and PRs (per GitHub docs: "GitHub's REST API considers every pull request an issue"). There is no server-side parameter to filter PRs out. The `issues` resource uses a dlt `processing_steps` filter to drop rows client-side where the `pull_request` key is present, before loading into DuckDB. This means PR data is fetched from `/issues` and discarded; the dedicated `/pulls` endpoint provides the canonical PR data. The overhead is minimal since both endpoints paginate in bulk (100 items per page).

**Repo-level comment endpoints**: Uses repo-level comment endpoints (not per-issue/per-PR) to avoid N+1 API calls. Comments have an `issue_url` field for joining back to parent items.

**Incremental limitation for `/pulls`**: The pulls endpoint has no `since` query parameter, so incremental loading relies on client-side cursor filtering after fetching. With `sort=updated&direction=desc`, recent items come first, but dlt may still paginate through all results. Acceptable for most repos; a known limitation for very large ones.

**Write disposition**: `merge` (upsert on `id` primary key) so updates to existing items are reflected.

**`--full` semantics**: Deletes `.ghtriage/ghtriage.duckdb` and `.ghtriage/pipelines/` entirely, then runs a fresh pull. This guarantees no stale rows from deleted upstream items.

dlt will auto-unnest nested arrays into child tables (e.g., `issues__labels`, `issues__assignees`, `pulls__requested_reviewers`).

## Key Config Details

- Pipeline `pipelines_dir` set to `.ghtriage/pipelines/` to keep state local
- `dataset_name="github"` so tables live in a `github` schema
- `query.py` runs `SET schema = 'github'` before user SQL so unqualified names work
- Git remote parsing handles both SSH (`git@github.com:o/r.git`) and HTTPS formats

## Testing Strategy

The codebase has two distinct kinds of logic with different testing needs:

**Pure functions (unit-testable):** `config.py` contains deterministic logic — git remote URL parsing, token/repo resolution precedence, and local file parsing. These are easy to test in isolation with pytest, no mocking needed beyond environment variables and temp files. This is where unit tests provide the most value.

**dlt pipeline + DuckDB queries (integration-testable, but costly):** `pipeline.py` and `query.py` interact with external systems (GitHub API, DuckDB). Real integration tests would require API credentials, network access, and take minutes to run. Mocking the GitHub API responses to test the dlt config is possible but brittle — we'd essentially be testing dlt's internals, not our code. The dlt REST API source config is declarative (a dict), so there isn't much logic to unit test.

**Recommendation:** Unit test `config.py` thoroughly with pytest. Validate the pipeline and query layer manually against a real repo (`drivendataorg/cloudpathlib`) during development. Defer pipeline integration tests — the ROI is low for a v1 and the manual validation steps catch real issues (wrong column names, missing tables, broken pagination) that mocked tests wouldn't.

**What to test in `tests/test_config.py`:**
- `parse_git_remote()` — SSH URLs, HTTPS URLs, HTTPS with `.git` suffix, non-GitHub URLs (should error), malformed URLs
- `resolve_token()` — env var present, env var missing + token file present, both missing (should error)
- `resolve_repo()` — CLI override, config.toml default, git remote fallback, precedence order
- token file parsing — strips whitespace and handles empty file as missing

## Phase 1: Core Pipeline

Goal: Get `ghtriage pull` working end-to-end and validate that data lands correctly in DuckDB. This is the riskiest phase — the dlt REST API source config may need iteration.

### Steps

1. **`pyproject.toml`** — add `dlt[duckdb]>=1.0,<2` dependency
2. **`src/ghtriage/config.py`** — `resolve_token()`, `resolve_repo()`, `parse_git_remote()`, `get_ghtriage_dir()`, `get_db_path()`, `get_pipelines_dir()`, token file parsing
3. **`src/ghtriage/pipeline.py`** — REST API source config dict, `create_pipeline()`, `run_pull()`
4. **`src/ghtriage/cli.py`** — argparse with `pull` subcommand only (+ stub `query`/`schema`)
5. **`src/ghtriage/__init__.py`** — wire `main()` to `cli.run()`

### Validate

```
uv run ghtriage pull --repo drivendataorg/cloudpathlib
```

Then inspect the DuckDB file directly to understand what dlt actually produced — table names, column names, child tables. This informs Phase 2.

---

## Phase 2: Query Interface + Polish

Goal: Build the query and schema commands on top of the confirmed data model from Phase 1.

### Steps

1. **`src/ghtriage/query.py`** — `execute_query()`, `get_tables()`, `get_table_columns()`
2. **Wire `query` and `schema` subcommands** in `cli.py`
3. **Output formatting** — table (column-aligned), csv (`csv.writer`), json (JSONL) for `query` command
4. **Error handling** — clear messages for no DB file, unknown table, SQL errors, and unsupported output format
5. **Schema ergonomics** — default schema to `github`, and decide whether to hide `_dlt_*` tables in `schema` output
6. **Unit tests** for query/schema/CLI flows — query execution, output formats, schema introspection, and error paths

### Validate

```
uv run ghtriage schema
uv run ghtriage schema --table issues
uv run ghtriage query "SELECT number, title, state FROM issues WHERE state='open' LIMIT 5"
uv run ghtriage query "SELECT count(*) FROM issues" --format json
```

## Files Modified

- `pyproject.toml` (add dependency) — Phase 1
- `src/ghtriage/__init__.py` (update `main()`) — Phase 1
- `src/ghtriage/config.py` (new) — Phase 1
- `src/ghtriage/pipeline.py` (new) — Phase 1
- `src/ghtriage/cli.py` (new) — Phase 1, extended in Phase 2
- `src/ghtriage/query.py` (new) — Phase 2
- `tests/test_config.py` (new) — Phase 2

---

## Phase 1 Execution Checklist

### 1) Scope and setup

- [x] Confirm Phase 1 scope is limited to dependency + `pull` flow + `query`/`schema` stubs
- [x] Create branch `codex/phase1-core-pipeline`
- [x] Capture baseline CLI behavior (`uv run python -m ghtriage --help`)

### 2) Dependency and environment

- [x] Add `dlt[duckdb]>=1.0,<2` to `pyproject.toml`
- [x] Run `uv sync`
- [x] Verify import smoke test (`uv run python -c "import dlt"`)

### 3) Config layer (`src/ghtriage/config.py`)

- [x] Implement `.ghtriage` path helpers:
  - [x] `get_ghtriage_dir()`
  - [x] `get_db_path()`
  - [x] `get_pipelines_dir()`
- [x] Implement GitHub repo parsing:
  - [x] `parse_git_remote()` for SSH + HTTPS (`.git` and non-`.git`)
- [x] Implement auth resolution:
  - [x] `resolve_token()` with precedence `env var > .ghtriage/token > error`
  - [x] token file parser strips whitespace and treats empty file as missing
- [x] Implement repo resolution:
  - [x] `resolve_repo()` with precedence `--repo > .ghtriage/config.toml > git remote`

### 4) Pipeline layer (`src/ghtriage/pipeline.py`)

- [x] Define dlt REST API source config for:
  - [x] `issues` (`/issues`, incremental `updated_at`, `since`, exclude PR rows)
  - [x] `pulls` (`/pulls`, incremental cursor on `updated_at`, no server-side `since`)
  - [x] `issue_comments` (`/issues/comments`, incremental `updated_at`, `since`)
  - [x] `pull_comments` (`/pulls/comments`, incremental `updated_at`, `since`)
- [x] Configure write disposition as `merge` with primary key `id`
- [x] Implement `create_pipeline()`:
  - [x] `dataset_name="github"`
  - [x] `pipelines_dir=.ghtriage/pipelines/`
- [x] Implement `run_pull()` to execute a pull for resolved repo/token
- [x] Implement `--full` behavior:
  - [x] Delete `.ghtriage/ghtriage.duckdb` (if present)
  - [x] Delete `.ghtriage/pipelines/` (if present)
  - [x] Run fresh pull

### 5) CLI wiring (`src/ghtriage/cli.py`, `src/ghtriage/__init__.py`)

- [x] Implement argparse root command with subcommands:
  - [x] `pull` (fully wired)
  - [x] `query` (stub with clear "not implemented" message)
  - [x] `schema` (stub with clear "not implemented" message)
- [x] Wire package entrypoint so `main()` calls `cli.run()`

### 6) Validation (real-world)

- [x] Run initial pull:
  - [x] `uv run ghtriage pull --repo drivendataorg/cloudpathlib`
- [x] Verify local artifacts exist:
  - [x] `.ghtriage/ghtriage.duckdb`
  - [x] `.ghtriage/pipelines/`
- [x] Inspect DuckDB tables/columns created by dlt (for Phase 2 query/schema design)
- [x] Run second pull to confirm incremental behavior works nominally

### 7) Quality gate

- [x] Run formatting (`just format`)
- [x] Run linting (`just lint`)
- [x] Smoke test CLI flows (`pull`, plus stub behavior for `query`/`schema`)
- [x] Record known limitations discovered during validation (especially `/pulls` incremental limitations)
  - [x] Note: `/pulls` has no server-side `since` filter; incremental relies on client-side cursor filtering.
  - [x] Note: first run should omit `since` for `/issues` and repo comment endpoints; using very old `since` can return zero rows on some repos.

### Phase 1 Definition of Done

- [x] `ghtriage pull` works end-to-end against a real public repo
- [x] Data is written to DuckDB in the `github` schema with expected top-level tables
- [x] Token and repo resolution precedence works as designed
- [x] `--full` reliably rebuilds from clean local state
- [x] CLI entrypoint is stable and ready for Phase 2 (`query`/`schema`) work

---

## Phase 2 Execution Checklist

### For Every PR (Recurring)

Run formatting on touched files (`just format ...`), run linting (`just lint ...`), and run targeted tests (`just test ...`) before opening/updating the PR.

### 1) Scope and preflight

- [x] Confirm scope is limited to implementing `query` + `schema` (no pipeline behavior changes)
- [x] Confirm auth behavior remains `GITHUB_TOKEN` env var, then `.ghtriage/token`
- [x] Capture current stub behavior for baseline (`uv run ghtriage query "SELECT 1"`, `uv run ghtriage schema`)
- [x] Confirm local Phase 1 artifacts are present (`.ghtriage/ghtriage.duckdb`, `.ghtriage/pipelines/`)

### 2) Query module implementation (`src/ghtriage/query.py`)

- [x] Add DuckDB connection helper that targets `.ghtriage/ghtriage.duckdb`
- [x] Fail with clear error when DB file does not exist
- [x] Implement `execute_query(sql)` and set schema with `SET schema = 'github'`
- [x] Implement `get_tables()` and `get_table_columns(table_name)` introspection helpers
- [x] Decide behavior for internal `_dlt_*` tables in schema output (hide by default in `get_tables()`, optional include via flag)

### 3) CLI wiring (`src/ghtriage/cli.py`)

- [x] Replace `query` stub with real execution path
- [x] Replace `schema` stub with table/column output path
- [x] Implement output formatters for query results:
  - [x] `table` (column-aligned)
  - [x] `csv`
  - [x] `json` (JSONL)
- [x] Return stable non-zero exit codes for user-facing failures
- [x] Ensure error messages are actionable (missing DB, bad SQL, unknown table)

### 4) Tests (TDD, local-only)

- [x] Add `tests/test_query.py` for:
  - [x] query execution against temp DuckDB fixture
  - [x] `table` / `csv` / `json` output behavior (covered via CLI tests)
  - [x] schema introspection behavior
  - [x] missing DB and SQL error paths
- [x] Add/extend CLI tests to verify command dispatch and exit codes for `query`/`schema`
- [x] Keep tests offline (no GitHub API calls)

### 5) Validation and quality gate

- [x] Manual smoke checks:
  - [x] `uv run ghtriage schema`
  - [x] `uv run ghtriage schema --table issues`
  - [x] `uv run ghtriage query "SELECT number, title, state FROM issues LIMIT 5"`
  - [x] `uv run ghtriage query "SELECT count(*) AS n FROM issues" --format json`

### 6) Documentation and release readiness

- [x] Update `README.md` examples to match actual Phase 2 command behavior
- [x] Record known limitations discovered during Phase 2 validation
  - [x] Note: `--format table` prints full values by design, so wide columns can be hard to scan in narrow terminals.
- [x] Confirm no stale "Phase 2 planned" stub messages remain in user-facing CLI output

### Phase 2 Risks and Gaps to Track

- [x] **Schema noise risk:** `_dlt_*` tables are hidden by default in `schema` output.
- [x] **Output readability risk:** accepted tradeoff for now; `table` output intentionally prints full values.
- [x] **Error UX risk:** missing DB, SQL errors, and unknown table errors return non-zero with actionable messages.
- [x] **Docs drift risk:** README and PLAN updated to match implemented command behavior.

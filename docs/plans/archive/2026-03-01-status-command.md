# Plan: `ghtriage status` Command

## Context

After a `pull`, users (and coding agents querying the DB) need a quick way to understand the state of the local database: what repo is in it, how fresh the data is, and how many rows each table has. This avoids needing to run `schema` + several `query` calls to piece together the same picture.

## Key Design Constraint: Two Repo Concepts

The "resolved repo" from `resolve_repo()` (drawn from `--repo`, `config.toml`, or git remote) reflects what the **next `pull` would target** — not necessarily what is **in the database**. These can diverge:

- You ran `ghtriage pull --repo owner/repo-A`, then changed `config.toml` to `repo-B`
- You ran `ghtriage pull` in one directory and are running `status` from another
- You manually edited `.ghtriage/config.toml`

`status` must distinguish between these two things and warn when they differ.

| Concept | Source | Meaning |
|---|---|---|
| **DB repo** | `_ghtriage_meta` table in DuckDB | What was actually pulled into the DB |
| **Config repo** | `resolve_repo()` | What the next `pull` would target |

## Metadata Table: `_ghtriage_meta`

Introduce a lightweight `_ghtriage_meta` table in the `github` DuckDB schema. `pipeline.py` writes to it after each successful load. `status` reads from it.

**Schema:**

```sql
CREATE TABLE IF NOT EXISTS github._ghtriage_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);
```

**Keys written on each `pull`:**

| Key | Value | Example |
|---|---|---|
| `repo` | `OWNER/REPO` slug | `drivendataorg/cloudpathlib` |
| `last_pull_at` | ISO 8601 UTC timestamp | `2026-02-28T14:22:57Z` |
| `last_full_pull` | `"true"` or `"false"` | `false` |

This is a deliberate contract — not a scrape of dlt internals — so it's stable across dlt version changes. Writing it is cheap (a single upsert after a successful load).

## Output Design

```
$ ghtriage status

Config repo:  drivendataorg/cloudpathlib  (from config.toml)
DB repo:      drivendataorg/cloudpathlib
Token:        GITHUB_TOKEN (env)
Database:     .ghtriage/ghtriage.duckdb (45.2 MB)
Last pull:    2026-02-28 14:22:57 UTC

Table               Rows    Latest updated_at
------------------  ------  --------------------
issues              234     2026-02-27 18:03:12
pulls               78      2026-02-26 09:14:55
issue_comments      1,832   2026-02-27 18:03:10
pull_comments       412     2026-02-25 22:41:03
```

If config repo and DB repo differ, insert a warning line:

```
Config repo:  owner/repo-B           (from config.toml)
DB repo:      owner/repo-A
WARNING: Config repo does not match DB repo. Next pull will target repo-B.
         Run `ghtriage pull --full` to rebuild for repo-B.
```

Child tables (`issues__labels`, `issues__assignees`, `pulls__requested_reviewers`, etc.) are omitted from the table summary — they add noise and their row counts aren't independently useful at a glance.

### Graceful Degradation

`status` should never hard-error. If a piece of information is unavailable, say so and continue:

| Situation | Behavior |
|---|---|
| DB does not exist | Skip DB repo, last pull, and table summary; note "not yet pulled" |
| `resolve_repo()` fails (no config, no remote) | Show "config repo: unknown" and continue |
| Token not configured | Show "token: not configured" and continue |

`status` must never require `--repo` or a valid token to produce useful output.

## Module Changes

### `pipeline.py`

Add a `_write_meta(conn, repo, full)` helper that upserts into `github._ghtriage_meta`. Call it from `run_pull()` after a successful dlt load, passing an open DuckDB connection.

### `query.py`

Add `get_status_data()` returning a dataclass (or plain dict) with:

- `db_path: Path`
- `db_size_bytes: int`
- `db_repo: str | None` (from `_ghtriage_meta`, `None` if missing)
- `last_pull_at: str | None` (from `_ghtriage_meta`, `None` if missing)
- `last_full_pull: bool | None`
- `table_stats: list[tuple[str, int, str | None]]` — `(table_name, row_count, max_updated_at)`

Table stats cover the four main resource tables only. `max_updated_at` is `None` for tables without that column (shouldn't happen for the main four, but handle it).

### `config.py`

Add `resolve_token_source()` (or extend `resolve_token()` to return a named tuple with `token` + `source: str`) so the CLI can label where the token came from (`env`, `token file`, or `not configured`).

### `cli.py`

Add `_run_status()` and wire a `status` subcommand. No additional flags needed for v1. Output is plain text to stdout; non-zero exit only on unexpected exceptions.

## Implementation Checklist

### 1) Metadata write (`pipeline.py`)

- [ ] Create `_ghtriage_meta` table if it does not exist (idempotent DDL in `run_pull()`)
- [ ] Upsert `repo`, `last_pull_at`, `last_full_pull` after successful dlt load
- [ ] Confirm the write does not interfere with dlt's own DuckDB usage (open a separate connection after dlt closes its own)

### 2) Token source (`config.py`)

- [ ] Extend or add alongside `resolve_token()` to expose which source the token came from
- [ ] Return `"not configured"` instead of raising when called from status context

### 3) Status data query (`query.py`)

- [ ] Implement `get_status_data()` with DB file size + mtime from filesystem
- [ ] Query `_ghtriage_meta` for DB repo and last pull time (handle missing table gracefully)
- [ ] Query row counts + `max(updated_at)` for the four main tables (handle missing table gracefully)
- [ ] Keep all DuckDB access in a single connection to avoid repeated open/close

### 4) CLI wiring (`cli.py`)

- [ ] Add `status` subcommand (no arguments)
- [ ] Implement `_run_status()`: resolve config repo + token source, call `get_status_data()`, format and print
- [ ] Print config/DB repo mismatch warning when applicable
- [ ] Graceful degradation for each missing piece (no unhandled exceptions)

### 5) Tests

- [ ] Unit test `get_status_data()` against a temp DuckDB fixture
  - [ ] With `_ghtriage_meta` present (normal post-pull state)
  - [ ] With missing DB (returns `None` or raises in a handled way)
- [ ] CLI test for `status` output format and mismatch warning path
- [ ] Test that `run_pull()` writes expected values to `_ghtriage_meta`

### 6) Validation

- [ ] `uv run ghtriage status` against a real pulled DB
- [ ] Manually verify mismatch warning by temporarily changing `config.toml`
- [ ] Run `ghtriage status` with no DB present and confirm graceful output
- [ ] Run formatting and linting (`just format`, `just lint`)

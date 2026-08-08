# ghtriage

**A local, queryable snapshot of a GitHub repository's issues, pull requests, and comments. Built for AI coding agents to assist with project management.**

**ghtriage** pulls a repository's GitHub data into a local DuckDB database that an agent can cheaply and quickly query with SQL. The motivating use case is project management and triage: an agent asked to find stale issues, likely duplicates, or unanswered questions can answer from the local database in milliseconds instead of paging through the GitHub API, complemented by the commit history already available in the local Git repository.

The command-line interface (CLI) provides commands for:

- pulling all issue, pull request, and comment data for a GitHub repository into the local database
- showing the state and freshness of the local database
- inspecting the database schema, including column documentation
- querying the database with SQL

ghtriage deliberately provides data and access, not judgments. It pre-computes facts that are awkward to derive from the raw tables, but it never scores, ranks, or decides what "stale" or "needs attention" means—that's the user's job to define within the context of their project.

## Installation

ghtriage is not yet published to PyPI. Install it as a CLI tool from GitHub with [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/jayqi/ghtriage
```

Or run it without installing:

```bash
uvx --from git+https://github.com/jayqi/ghtriage ghtriage --help
```

Any tool that installs from a Git URL (`pip`, `pipx`) also works. Requires Python 3.13+.

## Usage

### Setup

ghtriage needs a GitHub token to pull data. Set the `GITHUB_TOKEN` environment variable (e.g., from `gh auth token`), or write a token to the `.ghtriage/token` file.

By default, `pull` targets the repository of the current directory's git `origin` remote. To target a different repository, use the `--repo` flag, or set a default in `.ghtriage/config.toml`:

```toml
[repo]
default = "OWNER/REPO"
```

### Commands

```bash
ghtriage pull [--repo OWNER/REPO] [--full]
ghtriage status
ghtriage schema [--table TABLE_NAME]
ghtriage query "SQL statement" [--format table|csv|json]
```

### Query formats

- `table`: column-aligned text output with full values.
- `csv`: header row followed by CSV rows.
- `json`: strict JSONL (one JSON object per row).

### Examples

```bash
ghtriage schema
ghtriage schema --table issues
ghtriage schema --table issue_activity
ghtriage query "SELECT number, title, state FROM issues LIMIT 5"
ghtriage query "SELECT count(*) AS n FROM issues" --format json
ghtriage query "SELECT number, title FROM issue_activity WHERE state = 'open' AND first_non_author_comment_at IS NULL"
ghtriage query "SELECT number, title, round(score, 2) AS score FROM (SELECT *, fts_github_issue_threads.match_bm25(id, 'cache invalidation') AS score FROM issue_threads) t JOIN issue_activity USING (number) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 5"
```

### Exit codes

- `0`: command completed successfully.
- `1`: runtime failure (for example missing database, SQL error, unknown table).
- `2`: command usage/argument error (argparse-level failure).

## How it works

`pull` fetches data from the GitHub REST API and writes it to a DuckDB database in a `.ghtriage/` directory in the current working directory:

```
.ghtriage/
├── config.toml      # configuration, e.g., default repository (committable)
├── token            # GitHub token, if not using the GITHUB_TOKEN env var
├── ghtriage.duckdb  # the DuckDB database
└── pipelines/       # incremental pull state
```

The directory manages its own `.gitignore` so that only `config.toml` can be committed to version control; the token, database, and pull state are automatically excluded.

Some behaviors to be aware of:

- **The database is a snapshot.** It reflects GitHub as of the last `pull` and never updates on its own. Use `status` to see what repository is in the database and how fresh the data is.
- **Pulls are incremental.** Re-running `pull` fetches only what changed since the last pull, so it is cheap to run often. Use `--full` to delete the database and rebuild from scratch.
- **The target repository is resolved automatically.** In order of precedence: the `--repo` flag, the default set in `.ghtriage/config.toml`, then the current repository's git `origin` remote.

### What gets pulled

| Table | Contents |
|---|---|
| `issues` | Issues only. Pull requests are filtered out, though GitHub's endpoint returns both. |
| `pull_requests` | Pull requests. |
| `conversation_comments` | Comments on the main thread of the issue or pull request. |
| `review_comments` | Inline comments on a pull request's diff. |

Nested arrays become child tables named with a double-underscore, e.g., `issues__labels`, and can be joined to their parent on `_dlt_parent_id = _dlt_id`.

If any entity has zero records, then no table will be created rather than an empty. Run `ghtriage schema` for the authoritative list of what your
database actually holds.

### Derived tables and views

Every `ghtriage pull` also builds four derived objects. All are rebuilt each time the data refreshes, and every column carries a description you can read with `ghtriage schema --table <name>`.

- **`issue_activity`** — one row per issue, with comment counts and timestamps, labels, and assignees already joined.
- **`pull_request_activity`** — one row per pull request, the same plus review-comment facts and pending review requests.
- **`issue_threads`** — one full-text document per issue: its title, body, and every conversation comment on it, oldest first.
- **`pull_request_threads`** — one full-text document per pull request, folding in both conversation and review comments.

The thread tables exist to be searched — see [Full-text search](#full-text-search).

Details worth knowing:

- **A repository with zero issues or zero pull requests will not get the respective view or thread table.** Each is built from a base table, and there is no table until at least one record of that kind has been pulled. Query `ghtriage schema` to see what exists rather than assuming.
- **Pull requests have two separate comment channels.** GitHub's issue-comments endpoint carries conversation comments on both issues and pull requests; the pull-comments endpoint carries only inline review comments. `pull_request_activity` keeps them as separate columns — a PR can have a long discussion and no code review, or the reverse — while `pull_request_threads` folds them together.
- **Bot activity is split out, not filtered.** `comment_count` counts everything; `non_bot_comment_count` counts only accounts GitHub does not type as `Bot`. Subtract for the bot count. The same pattern applies to review comments and participants.

### Full-text search

Every pull also builds BM25 full-text indexes, so "has this been reported before?" is a ranked query rather than a scan. Indexed are `issues` and `pull_requests` (title and body), both comment tables (body), and the two thread tables (`thread_text`).

Search a thread table to find related issues and pull requests; search a comment table to find individual comments. The comment indexes score each comment on its own, so a thread that circles a topic across several replies ranks below a single comment that says it all.

Each index lives in a schema named for its table — `fts_github_issues`, `fts_github_issue_threads` — and exposes `match_bm25(id, 'query')`:

```bash
ghtriage query "SELECT number, title, round(score, 2) AS score FROM (SELECT *, fts_github_issue_threads.match_bm25(id, 'timeout uploading large files') AS score FROM issue_threads) t JOIN issue_activity USING (number) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10"
```

Run `ghtriage schema` for the indexes your database actually holds, with their columns and document counts. Things worth knowing:

- **The score function is keyed on the document id, not bound to its table.** Both derived views carry `id`, so you can search and filter on derived facts in one query with no join: `SELECT * FROM (SELECT *, fts_github_issues.match_bm25(id, 'windows path') AS score FROM issue_activity) WHERE score IS NOT NULL AND state = 'open'`.
- **Pairing a table with the wrong index returns nothing rather than an error.** An id the index does not hold simply scores `NULL`. If a search comes back empty, check the index name before concluding the repository has nothing.
- **Digits are not indexed.** The tokenizer strips them, so searching `404` finds nothing. Use `LIKE` or `regexp_matches` for exact codes, versions, and identifiers.
- **Terms are OR-ed and stemmed by default.** `'azure credential'` matches documents containing either word, and `renaming` matches `rename`. Pass `conjunctive := 1` to require every term.
- **Indexes are rebuilt from scratch on every pull**, so they are exactly as fresh as the data. On a repository with ~2,500 documents this adds about a third of a second to a pull and about 35% to the database file.

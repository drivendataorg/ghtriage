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
ghtriage query "SELECT number, title, state FROM issues LIMIT 5"
ghtriage query "SELECT count(*) AS n FROM issues" --format json
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

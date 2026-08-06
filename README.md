# ghtriage

**A local, queryable snapshot of a GitHub repository's issues, pull requests, and comments. Built for AI coding agents to assist with project management.**

**ghtriage** pulls a repository's GitHub data into a local DuckDB database that an agent can cheaply and quickly query with SQL. The motivating use case is project management and triage: an agent asked to find stale issues, likely duplicates, or unanswered questions can answer from the local database in milliseconds instead of paging through the GitHub API, complemented by the commit history already available in the local Git repository.

The command-line interface (CLI) provides commands for:

- pulling all issue, pull request, and comment data for a GitHub repository into the local database
- showing the state and freshness of the local database
- inspecting the database schema, including column documentation
- querying the database with SQL

ghtriage deliberately provides data and access, not judgments. It pre-computes facts that are awkward to derive from the raw tables, but it never scores, ranks, or decides what "stale" or "needs attention" means—that's the user's job to define within the context of their project.

## Commands

```bash
ghtriage pull [--repo OWNER/REPO] [--full]
ghtriage schema [--table TABLE_NAME]
ghtriage query "SQL statement" [--format table|csv|json]
```

## Query formats

- `table`: column-aligned text output with full values.
- `csv`: header row followed by CSV rows.
- `json`: strict JSONL (one JSON object per row).

## Examples

```bash
uv run ghtriage schema
uv run ghtriage schema --table issues
uv run ghtriage query "SELECT number, title, state FROM issues LIMIT 5"
uv run ghtriage query "SELECT count(*) AS n FROM issues" --format json
```

## Exit codes

- `0`: command completed successfully.
- `1`: runtime failure (for example missing database, SQL error, unknown table).
- `2`: command usage/argument error (argparse-level failure).

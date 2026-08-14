import argparse
import csv
from getpass import getpass
import json
import os
from pathlib import Path
import sys
from typing import Sequence

from ghtriage.config import (
    ConfigError,
    GhtriageConfig,
    enable_gh_token_fallback,
    get_db_path,
    get_ghtriage_dir,
    gh_cli_token,
    load_config,
    resolve_repo,
    resolve_token,
    token_sources,
)
from ghtriage.pipeline import SchemaGenerationMismatch, run_pull
from ghtriage.query import (
    FullTextIndex,
    execute_query,
    get_full_text_indexes,
    get_status_data,
    get_table_columns,
    get_table_descriptions,
    get_tables,
)
from ghtriage.skill_install import SkillInstallError, install_skill, resolve_destination


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

    auth_parser = subparsers.add_parser("auth", help="Set up and inspect GitHub authentication")
    # metavar, so the missing-subcommand error names the choices, not the dest.
    auth_subparsers = auth_parser.add_subparsers(
        dest="auth_command", metavar="{setup,status}", required=True
    )
    auth_setup_parser = auth_subparsers.add_parser(
        "setup", help="Choose an authentication method and save a token"
    )
    auth_setup_parser.add_argument(
        "--use-gh-token",
        action="store_true",
        help="Skip the menu and enable the gh CLI fallback (`gh auth token`)",
    )
    auth_subparsers.add_parser("status", help="Show every token source and which one is used")

    skill_parser = subparsers.add_parser("skill", help="Manage the ghtriage agent skill")
    # metavar, so the missing-subcommand error names the choices, not the dest.
    skill_subparsers = skill_parser.add_subparsers(
        dest="skill_command", metavar="{install}", required=True
    )
    skill_install_parser = skill_subparsers.add_parser(
        "install", help="Copy the skill into an agent's skills directory"
    )
    skill_install_parser.add_argument(
        "--agent",
        choices=("claude-code", "universal"),
        help="Which agent's skills directory to target; prompts when omitted",
    )
    skill_install_parser.add_argument(
        "--scope",
        choices=("project", "user"),
        help="Install under the current directory (default) or the home directory",
    )
    skill_install_parser.add_argument(
        "--dir",
        help="Install to DIR/ghtriage/ instead; cannot be combined with --agent or --scope",
    )
    skill_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace a skill directory ghtriage did not install",
    )

    return parser


def _auth_http_status(exc: BaseException) -> int | None:
    """The auth-shaped HTTP status in the chain dlt raises, if there is one.

    dlt wraps the requests.HTTPError (which carries its Response) in
    ResourceExtractionError and PipelineStepFailed, chained via `from`.
    """
    current: BaseException | None = exc
    while current is not None:
        candidate = getattr(getattr(current, "response", None), "status_code", None)
        if candidate in (401, 403, 404):
            return candidate
        current = current.__cause__ or current.__context__
    return None


def _print_auth_failure_guidance(status_code: int, repo: str) -> None:
    print(f"GitHub rejected the request (HTTP {status_code}).", file=sys.stderr)
    print("Check which token ghtriage is using: ghtriage auth status", file=sys.stderr)
    if status_code == 401:
        print(
            "The token is invalid or expired. Save a new one: ghtriage auth setup",
            file=sys.stderr,
        )
        return
    if status_code == 403:
        print(
            f"The token was refused. If {repo} belongs to an organization that enforces",
            file=sys.stderr,
        )
        print(
            'SAML SSO, authorize the token for that organization ("Configure SSO" on',
            file=sys.stderr,
        )
        print(
            "https://github.com/settings/tokens). A 403 can also mean the API rate limit",
            file=sys.stderr,
        )
        print("is exhausted -- wait and retry.", file=sys.stderr)
        return
    print(f"Check that {repo} exists and is spelled correctly.", file=sys.stderr)
    print(
        "If it does: GitHub returns 404 for anything a token cannot see, so if it is a private,",
        file=sys.stderr,
    )
    print(
        "org-owned repository, a fine-grained token may still be awaiting org approval. Ask an",
        file=sys.stderr,
    )
    print(
        "org admin to approve it, or create a classic token instead: ghtriage auth setup",
        file=sys.stderr,
    )


def _run_pull(args: argparse.Namespace) -> int:
    # One load, shared by both resolutions: config errors raise here, once, and the
    # loader's unrecognized-key warnings print once.
    config = load_config()
    repo = resolve_repo(cli_repo=args.repo, config=config)
    token = resolve_token(config=config)
    if token is None:
        print(
            "Missing GitHub token. Run `ghtriage auth setup` to set one up.",
            file=sys.stderr,
        )
        print(
            "The GITHUB_TOKEN environment variable or a .ghtriage/token file also work.",
            file=sys.stderr,
        )
        return 1
    try:
        load_info, warnings = run_pull(repo=repo, token=token, full=args.full)
    except SchemaGenerationMismatch as exc:
        print(exc, file=sys.stderr)
        return 1
    except Exception as exc:
        status_code = _auth_http_status(exc)
        if status_code is None:
            # Not an auth failure: stays a loud error rather than a summarized one.
            raise
        print(f"Pull failed for {repo}: {exc}", file=sys.stderr)
        _print_auth_failure_guidance(status_code, repo)
        return 1
    print(f"Pull completed for {repo}")
    print(load_info)
    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)
    if warnings:
        # Every post-load step is recomputed from scratch by a full pull, so one command
        # covers all of them. Said here rather than in each warning, and without assuming
        # the reader knows this repository.
        print(
            "A full refresh rebuilds everything the failed steps produce: ghtriage pull --full",
            file=sys.stderr,
        )
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
    """The searchable surface. An index agents cannot discover may as well not exist."""
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
                index.key_column,
                ", ".join(index.columns),
                f"{index.document_count:,}",
            )
            for index in indexes
        ],
    )
    # Declaration order, so the example is `issues` -- which reads as an example in a
    # way that a comment table does not.
    example = indexes[0]
    print()
    print("Search a table by scoring its key column against its own index:")
    # The subquery form, not `WHERE` on a bare alias: the projection keeps a copied
    # example from returning whole bodies, and an alias in WHERE would silently bind to
    # a real `score` column if an indexed table ever grew one.
    print("  SELECT number, title, score FROM (")
    print(
        f"      SELECT *, fts_github_{example.table}."
        f"match_bm25({example.key_column}, 'search terms') AS score FROM {example.table}"
    )
    print("  ) WHERE score IS NOT NULL ORDER BY score DESC LIMIT 10")
    print()
    print(
        "The macro is keyed on the document id and is not bound to the indexed table, "
        "so it also works"
    )
    print("from any relation carrying that id. An id the index does not hold scores NULL.")


def _run_schema(args: argparse.Namespace) -> int:
    try:
        if args.table:
            indexes = [i for i in get_full_text_indexes() if i.table == args.table]
            if indexes:
                index = indexes[0]
                print(
                    f"Full-text search: fts_github_{index.table}."
                    f"match_bm25({index.key_column}, 'query') "
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
    # Loaded outside the try: a config the loader rejects must stay loud, or the one
    # command that reports state would disguise it as an ordinary unset repo.
    config = load_config()
    try:
        config_repo: str | None = resolve_repo(config=config)
    except (RuntimeError, ValueError, OSError):
        # No usable git remote: for status that is a state to report, not an error.
        config_repo = None

    db_path = get_db_path(create=False)
    try:
        display_db_path = db_path.relative_to(Path.cwd())
    except ValueError:
        display_db_path = db_path

    print(f"Config repo:  {config_repo or 'unknown'}")

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


AUTH_MENU = """\
How should ghtriage authenticate to GitHub?

  1. Fine-grained personal access token (recommended -- read-only, this repo only)
  2. Classic personal access token (broader access; for orgs that block
     fine-grained tokens)
  3. Reuse your gh CLI login (convenient; broader access)
"""


def _fine_grained_url(repo: str | None) -> str:
    """No network call decides these; the query string is all the prefill GitHub accepts."""
    if repo is None:
        return (
            "https://github.com/settings/personal-access-tokens/new"
            "?name=ghtriage&description=Read+issues+and+PRs+for+ghtriage"
            "&issues=read&pull_requests=read"
        )
    owner = repo.split("/")[0]
    return (
        "https://github.com/settings/personal-access-tokens/new"
        f"?name=ghtriage+({repo})&description=Read+issues+and+PRs+for+ghtriage"
        f"&target_name={owner}&issues=read&pull_requests=read"
    )


def _classic_url(repo: str | None) -> str:
    description = "ghtriage" if repo is None else f"ghtriage+({repo})"
    return f"https://github.com/settings/tokens/new?scopes=repo&description={description}"


def _print_fine_grained_instructions(repo: str | None) -> None:
    print("Create a fine-grained personal access token:")
    print()
    print(f"  {_fine_grained_url(repo)}")
    print()
    # The URL can prefill the permissions but cannot select the owner or the repository,
    # and an org that blocks fine-grained tokens is simply absent from the owner dropdown
    # -- which is the signal to come back and pick the classic token instead.
    if repo is None:
        print("  1. Resource owner: pick the account or organization that owns the repository.")
        print("     If it is not in the dropdown, that organization blocks fine-grained")
        print("     tokens -- re-run `ghtriage auth setup` and choose the classic token.")
        print('  2. Repository access: choose "Only select repositories" and pick the repo.')
    else:
        owner = repo.split("/")[0]
        print(f"  1. Resource owner: verify it says {owner}. GitHub defaults to your personal")
        print(f"     account, and if {owner} is not in the dropdown at all, that organization")
        print("     blocks fine-grained tokens -- re-run `ghtriage auth setup` and choose the")
        print("     classic token.")
        print(f'  2. Repository access: choose "Only select repositories" and pick {repo}.')
    print("  3. Permissions are prefilled: Issues (read-only) and Pull requests (read-only),")
    print("     which is everything ghtriage reads.")
    print("  4. Pick an expiration, then generate the token.")
    print()


def _print_classic_instructions(repo: str | None) -> None:
    print("Create a classic personal access token:")
    print()
    print(f"  {_classic_url(repo)}")
    print()
    print("  1. The `repo` scope is prefilled. The tradeoff: classic `repo` scope grants read")
    print("     and write access to every repository you can reach -- far more than ghtriage")
    print("     needs. Use it when your organization blocks fine-grained tokens, or approval")
    print("     of one is stuck.")
    print("  2. Pick an expiration, then generate the token.")
    print()


def _prompt_auth_choice() -> str | None:
    print(AUTH_MENU)
    try:
        choice = input("Choice [1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted. Nothing was saved.", file=sys.stderr)
        return None
    return choice or "1"


def _save_pasted_token(ghtriage_dir: Path, repo: str | None, *, classic: bool) -> int:
    if classic:
        _print_classic_instructions(repo)
    else:
        _print_fine_grained_instructions(repo)

    if os.environ.get("GITHUB_TOKEN"):
        print(
            "Note: GITHUB_TOKEN is set in this environment and takes precedence over the "
            "saved file."
        )
    token_path = ghtriage_dir / "token"
    if token_path.exists():
        print("Note: .ghtriage/token already exists; pasting replaces it.")

    try:
        # getpass writes its prompt to the terminal directly, so flush the instructions
        # above first -- otherwise a redirected stdout prints them after the prompt.
        sys.stdout.flush()
        token = getpass("Paste the token (input is hidden): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted. Nothing was saved.", file=sys.stderr)
        return 1

    if not token:
        print("No token entered. Nothing was saved.", file=sys.stderr)
        return 1

    # Created private before it holds anything, and re-chmodded in case it already existed.
    token_path.touch(mode=0o600, exist_ok=True)
    os.chmod(token_path, 0o600)
    token_path.write_text(f"{token}\n", encoding="utf-8")

    print(f"Saved the token to {token_path}.")
    print("Check what ghtriage will use with: ghtriage auth status")
    return 0


def _enable_gh_fallback(ghtriage_dir: Path) -> int:
    config_path = enable_gh_token_fallback()
    print(f"Enabled the gh CLI fallback: [auth] use_gh_token = true in {config_path}.")

    # The setting is the user's preference; whether gh can answer today is separate news.
    result = gh_cli_token()
    if result.token:
        print("`gh auth token` returned a token, so ghtriage can authenticate.")
    else:
        print(f"No token from the gh CLI yet: {result.error}", file=sys.stderr)
        print("The setting is saved; ghtriage will use gh once it can answer.", file=sys.stderr)
    # The fallback is last in precedence: a user who just chose gh must hear that
    # another source still wins, or their own setup is misreported back to them.
    if os.environ.get("GITHUB_TOKEN"):
        print(
            "Note: GITHUB_TOKEN is set in this environment and takes precedence over "
            "the gh fallback."
        )
    if (ghtriage_dir / "token").exists():
        print("Note: .ghtriage/token exists and takes precedence over the gh fallback.")
    print("Check what ghtriage will use with: ghtriage auth status")
    return 0


def _resolve_repo_for_links(config: GhtriageConfig) -> str | None:
    """The repo the printed links should target, or None for the generic links."""
    try:
        return resolve_repo(config=config)
    except (RuntimeError, ValueError, OSError):
        # No usable git remote: generic links still work, the instructions cover the rest.
        return None


def _run_auth_setup(args: argparse.Namespace) -> int:
    # Eagerly, so the directory and its scaffolded config.toml exist to hand-edit even if
    # the user abandons the prompt below.
    ghtriage_dir = get_ghtriage_dir(create=True)

    # Loaded on every path, before anything is written: the one command that writes the
    # config must not be the one that skips reading it, and a config the loader rejects
    # is refused here with the file left as the user had it.
    config = load_config()

    if args.use_gh_token:
        return _enable_gh_fallback(ghtriage_dir)

    choice = _prompt_auth_choice()
    if choice is None:
        return 1
    if choice == "1":
        return _save_pasted_token(ghtriage_dir, _resolve_repo_for_links(config), classic=False)
    if choice == "2":
        return _save_pasted_token(ghtriage_dir, _resolve_repo_for_links(config), classic=True)
    if choice == "3":
        return _enable_gh_fallback(ghtriage_dir)

    print(f"Not one of the choices: {choice}", file=sys.stderr)
    return 1


def _run_auth_status(args: argparse.Namespace) -> int:
    sources = token_sources()
    in_use = next((source for source in sources if source.token), None)

    name_width = max(len(source.name) for source in sources)
    state_width = max(len(source.state) for source in sources)
    for source in sources:
        trailing = "<- in use" if source is in_use else source.note
        line = f"{source.name.ljust(name_width)}  {source.state.ljust(state_width)}  {trailing}"
        print(line.rstrip())

    if in_use is None:
        print()
        print("No token configured. Run `ghtriage auth setup` to set one up.")
    return 0


SKILL_AGENT_MENU = """\
Install the skill for which agent?

  1. claude-code -- Claude Code (.claude/skills/)
  2. universal   -- Copilot, Cursor, Codex, Gemini CLI, and others (.agents/skills/)
"""

SKILL_AGENTS = {
    "1": "claude-code",
    "claude-code": "claude-code",
    "2": "universal",
    "universal": "universal",
}


def _prompt_agent_choice() -> str | None:
    """The chosen agent, or None when the answer was an abort or not a choice.

    Deliberately unlike the `auth setup` menu in one way: no default, on empty input or at
    all. There one method is genuinely recommended; here neither value is, because which is
    right is a fact about the agent the user runs. Guessing it installs into a directory
    that agent never reads -- silently useless, the worst failure this command has.
    """
    print(SKILL_AGENT_MENU)
    try:
        choice = input("Choice: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        print("Aborted. Nothing was installed.", file=sys.stderr)
        return None

    agent = SKILL_AGENTS.get(choice.lower())
    if agent is None:
        print(f"Not one of the choices: {choice}", file=sys.stderr)
    return agent


def _display_destination(destination: Path) -> str:
    try:
        display = destination.relative_to(Path.cwd())
    except ValueError:
        display = destination
    # Forward slashes on every platform: skill paths appear that way throughout the docs,
    # and a native-separator render would end `.claude\skills\ghtriage/` -- mixed.
    return f"{display.as_posix()}/"


def _run_skill_install(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.dir is not None and (args.agent is not None or args.scope is not None):
        parser.error("--dir cannot be combined with --agent or --scope")

    agent = args.agent
    if args.dir is None and agent is None:
        agent = _prompt_agent_choice()
        if agent is None:
            return 1

    destination = resolve_destination(
        agent=agent, scope=args.scope or "project", directory=args.dir
    )
    try:
        result = install_skill(destination, force=args.force)
    except SkillInstallError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not install the skill to {destination}: {exc}", file=sys.stderr)
        return 1

    display = _display_destination(result.destination)
    if result.action == "installed":
        print(f"Installed skill to {display} (ghtriage {result.version})")
    elif result.action == "unchanged":
        print(f"Skill already up to date at {display} ({result.version})")
    elif result.previous_version and result.previous_version != result.version:
        print(f"Replaced skill at {display} ({result.previous_version} -> {result.version})")
    else:
        print(f"Replaced skill at {display} (ghtriage {result.version})")
    return 0


def run(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "pull":
            return _run_pull(args)
        if args.command == "query":
            return _run_query(args)
        if args.command == "schema":
            return _run_schema(args)
        if args.command == "status":
            return _run_status(args)
        if args.command == "auth":
            if args.auth_command == "setup":
                return _run_auth_setup(args)
            if args.auth_command == "status":
                return _run_auth_status(args)
        if args.command == "skill":
            if args.skill_command == "install":
                return _run_skill_install(args, parser)
    except ConfigError as exc:
        # The one boundary for a bad config.toml: a hand-edit mistake is user-facing
        # news, not a traceback. Everything else that raises stays loud.
        print(exc, file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")

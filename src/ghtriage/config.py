from collections.abc import Iterator
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python <3.11
    import tomli as tomllib

import tomlkit

REPO_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOCAL_GITIGNORE_CONTENT = textwrap.dedent(
    """\
    *
    !.gitignore
    """
)
LOCAL_CONFIG_CONTENT = textwrap.dedent(
    """\
    # Repository to pull, as OWNER/REPO. If unset, ghtriage uses the
    # current directory's git "origin" remote.
    # repo = "OWNER/REPO"

    [auth]
    # Fall back to the gh CLI's token (`gh auth token`) when no GITHUB_TOKEN
    # env var or .ghtriage/token file is present. Convenient, but the gh CLI
    # token has broader permissions than a fine-grained PAT.
    # use_gh_token = true
    """
)


def _ensure_local_gitignore(ghtriage_dir: Path) -> None:
    gitignore_path = ghtriage_dir / ".gitignore"
    if gitignore_path.exists():
        return
    gitignore_path.write_text(LOCAL_GITIGNORE_CONTENT, encoding="utf-8")


def _ensure_local_config(ghtriage_dir: Path) -> None:
    """Scaffold config.toml with every key commented out, so the file is inert until edited."""
    config_path = ghtriage_dir / "config.toml"
    if config_path.exists():
        return
    config_path.write_text(LOCAL_CONFIG_CONTENT, encoding="utf-8")


def get_ghtriage_dir(cwd: str | Path | None = None, create: bool = True) -> Path:
    root = Path(cwd) if cwd is not None else Path.cwd()
    ghtriage_dir = root / ".ghtriage"
    if create:
        ghtriage_dir.mkdir(parents=True, exist_ok=True)
        _ensure_local_gitignore(ghtriage_dir)
        _ensure_local_config(ghtriage_dir)
    return ghtriage_dir


def get_db_path(cwd: str | Path | None = None, create: bool = True) -> Path:
    return get_ghtriage_dir(cwd=cwd, create=create) / "ghtriage.duckdb"


def get_pipelines_dir(cwd: str | Path | None = None) -> Path:
    return get_ghtriage_dir(cwd=cwd) / "pipelines"


def parse_git_remote(remote_url: str) -> str:
    remote_url = remote_url.strip()
    patterns = (
        re.compile(r"^git@github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"),
        re.compile(r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"),
        re.compile(r"^ssh://git@github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"),
    )

    for pattern in patterns:
        match = pattern.match(remote_url)
        if not match:
            continue
        owner = match.group("owner")
        repo = match.group("repo")
        slug = f"{owner}/{repo}"
        if REPO_SLUG_PATTERN.fullmatch(slug):
            return slug
        raise ValueError(f"Invalid GitHub repository slug: {slug}")

    if "github.com" in remote_url:
        raise ValueError(f"Unsupported GitHub remote URL format: {remote_url}")
    raise ValueError(f"Remote is not a GitHub URL: {remote_url}")


def _read_token_file(path: Path) -> str | None:
    if not path.exists():
        return None
    token = path.read_text(encoding="utf-8").strip()
    return token or None


@dataclass(frozen=True)
class GhtriageConfig:
    """Everything `.ghtriage/config.toml` can say. Absent keys keep their defaults."""

    repo: str | None = None
    use_gh_token: bool = False


@dataclass(frozen=True)
class GhTokenResult:
    """Outcome of `gh auth token`. `error` says why there is no token, for the user."""

    token: str | None = None
    error: str | None = None


def gh_cli_token() -> GhTokenResult:
    """Read the gh CLI's stored token. A missing or logged-out gh is a result, not a crash."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return GhTokenResult(error="gh CLI is not installed")
    if proc.returncode != 0:
        return GhTokenResult(error="gh CLI is not logged in (run `gh auth login`)")
    token = proc.stdout.strip()
    if not token:
        return GhTokenResult(error="gh CLI returned no token (run `gh auth login`)")
    return GhTokenResult(token=token)


@dataclass(frozen=True)
class TokenSource:
    """One place a token can come from, with everything `auth status` needs to render it."""

    name: str
    state: str
    token: str | None = None
    note: str = ""


def token_sources(
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    config: GhtriageConfig | None = None,
) -> list[TokenSource]:
    """Every source, resolved, in precedence order. `auth status` renders this."""
    return list(_iter_token_sources(cwd=cwd, env=env, config=config))


def _iter_token_sources(
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    config: GhtriageConfig | None = None,
) -> Iterator[TokenSource]:
    """Lazy, so `resolve_token` stops before spending a subprocess it does not need."""
    env_data = env if env is not None else os.environ
    env_token = env_data.get("GITHUB_TOKEN") or None
    yield TokenSource(
        name="GITHUB_TOKEN (env)",
        state="found" if env_token else "not set",
        token=env_token,
    )

    file_token = _read_token_file(get_ghtriage_dir(cwd=cwd, create=False) / "token")
    yield TokenSource(
        name=".ghtriage/token",
        state="found" if file_token else "not set",
        token=file_token,
    )

    if config is None:
        config = load_config(cwd=cwd)
    if not config.use_gh_token:
        yield TokenSource(
            name="gh auth token",
            state="disabled",
            note="(enable: ghtriage auth setup --use-gh-token)",
        )
        return

    result = gh_cli_token()
    yield TokenSource(
        name="gh auth token",
        state="available" if result.token else "not available",
        token=result.token,
        note="" if result.token else f"({result.error})",
    )


def resolve_token(
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    config: GhtriageConfig | None = None,
) -> str | None:
    """The winning token, or None; never raises for a token that is merely missing."""
    for source in _iter_token_sources(cwd=cwd, env=env, config=config):
        if source.token:
            return source.token
    return None


CONFIG_KEYS = ("repo",)
CONFIG_TABLES = {"auth": ("use_gh_token",)}


class ConfigError(RuntimeError):
    """A config.toml ghtriage cannot honor. The CLI reports these as messages, not
    tracebacks; anything else that raises stays a loud, unexpected error."""


def _warn_unrecognized(config_data: dict, config_path: Path) -> None:
    """The file is hand-edited, so a typo'd key would otherwise silently do nothing."""
    for key, value in config_data.items():
        if not isinstance(value, dict) and key in CONFIG_TABLES:
            # A scalar named after a known table is a type error load_config raises;
            # calling a recognized name "unrecognized" here would be false.
            continue
        if isinstance(value, dict):
            known_sub_keys = CONFIG_TABLES.get(key)
            if known_sub_keys is None:
                if key in CONFIG_KEYS:
                    # A table named after a known key is a type error load_config
                    # raises; an "ignores it" warning here would contradict that.
                    continue
                print(
                    f"Warning: unrecognized table [{key}] in {config_path}; ghtriage ignores it.",
                    file=sys.stderr,
                )
                continue
            for sub_key in value:
                if sub_key not in known_sub_keys:
                    print(
                        f"Warning: unrecognized key '{key}.{sub_key}' in {config_path}; "
                        "ghtriage ignores it.",
                        file=sys.stderr,
                    )
        elif key not in CONFIG_KEYS:
            print(
                f"Warning: unrecognized key '{key}' in {config_path}; ghtriage ignores it.",
                file=sys.stderr,
            )


def load_config(cwd: str | Path | None = None) -> GhtriageConfig:
    config_path = get_ghtriage_dir(cwd=cwd, create=False) / "config.toml"
    if not config_path.exists():
        return GhtriageConfig()
    try:
        with config_path.open("rb") as file_obj:
            config_data = tomllib.load(file_obj)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    _warn_unrecognized(config_data, config_path)

    repo_value = config_data.get("repo")
    repo: str | None = None
    if repo_value is not None:
        if not isinstance(repo_value, str):
            raise ConfigError(f"Invalid repo in {config_path}: expected a string")
        repo = repo_value.strip() or None
        if repo is not None and not REPO_SLUG_PATTERN.fullmatch(repo):
            raise ConfigError(f"Invalid repo in {config_path}: expected OWNER/REPO, got: {repo}")

    use_gh_token = False
    auth_data = config_data.get("auth")
    if auth_data is not None and not isinstance(auth_data, dict):
        raise ConfigError(f"Invalid auth in {config_path}: expected a table")
    if isinstance(auth_data, dict):
        use_gh_token_value = auth_data.get("use_gh_token")
        if use_gh_token_value is not None:
            if not isinstance(use_gh_token_value, bool):
                raise ConfigError(
                    f"Invalid [auth].use_gh_token in {config_path}: expected a boolean"
                )
            use_gh_token = use_gh_token_value

    return GhtriageConfig(repo=repo, use_gh_token=use_gh_token)


def enable_gh_token_fallback(cwd: str | Path | None = None) -> Path:
    """Set `[auth] use_gh_token = true` in place.

    The tool's one programmatic config write; tomlkit's round-trip keeps comments, layout,
    and hand edits intact, so this behaves the same on a fresh scaffold and an edited file.
    """
    config_path = get_ghtriage_dir(cwd=cwd, create=True) / "config.toml"
    try:
        document = tomlkit.parse(config_path.read_text(encoding="utf-8"))
    except tomlkit.exceptions.ParseError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    auth_table = document.get("auth")
    if not isinstance(auth_table, dict):
        auth_table = tomlkit.table()
        document["auth"] = auth_table
    auth_table["use_gh_token"] = True
    config_path.write_text(tomlkit.dumps(document), encoding="utf-8")
    return config_path


def _validate_repo_slug(repo: str) -> str:
    repo = repo.strip()
    if REPO_SLUG_PATTERN.fullmatch(repo):
        return repo
    raise ValueError(f"Repository must be in OWNER/REPO format, got: {repo}")


def get_git_remote_origin(cwd: str | Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        raise RuntimeError(f"Could not determine git origin remote: {stderr or 'unknown error'}")
    remote = proc.stdout.strip()
    if not remote:
        raise RuntimeError("Git origin remote is empty")
    return remote


def resolve_repo(
    cli_repo: str | None = None,
    cwd: str | Path | None = None,
    config: GhtriageConfig | None = None,
) -> str:
    if cli_repo:
        return _validate_repo_slug(cli_repo)

    if config is None:
        config = load_config(cwd=cwd)
    if config.repo:
        # Validated by load_config, the one boundary every config value crosses.
        return config.repo

    remote = get_git_remote_origin(cwd=cwd)
    return parse_git_remote(remote)

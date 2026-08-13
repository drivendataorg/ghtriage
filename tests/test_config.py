from pathlib import Path
import subprocess

import pytest

from ghtriage.config import (
    LOCAL_CONFIG_CONTENT,
    enable_gh_token_fallback,
    get_ghtriage_dir,
    gh_cli_token,
    load_config,
    parse_git_remote,
    resolve_repo,
    resolve_token,
    token_sources,
)


@pytest.mark.parametrize(
    ("remote_url", "expected"),
    [
        ("git@github.com:octocat/hello-world.git", "octocat/hello-world"),
        ("git@github.com:octocat/hello-world", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world.git", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world", "octocat/hello-world"),
        ("ssh://git@github.com/octocat/hello-world.git", "octocat/hello-world"),
    ],
)
def test_parse_git_remote_valid(remote_url: str, expected: str) -> None:
    assert parse_git_remote(remote_url) == expected


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://gitlab.com/octocat/hello-world.git",
        "git@github.com:octocat",
        "not-a-url",
    ],
)
def test_parse_git_remote_invalid(remote_url: str) -> None:
    with pytest.raises(ValueError):
        parse_git_remote(remote_url)


def test_get_ghtriage_dir_creates_local_gitignore(tmp_path: Path) -> None:
    """The whole directory is ignored, config.toml included -- it holds an auth preference."""
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path, create=True)
    gitignore_path = ghtriage_dir / ".gitignore"
    assert gitignore_path.exists()
    assert gitignore_path.read_text(encoding="utf-8") == "*\n!.gitignore\n"


def test_get_ghtriage_dir_leaves_an_existing_gitignore_alone(tmp_path: Path) -> None:
    ghtriage_dir = tmp_path / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / ".gitignore").write_text("hand edited\n", encoding="utf-8")

    get_ghtriage_dir(cwd=tmp_path, create=True)

    assert (ghtriage_dir / ".gitignore").read_text(encoding="utf-8") == "hand edited\n"


def test_get_ghtriage_dir_scaffolds_config_toml(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path, create=True)
    config_path = ghtriage_dir / "config.toml"
    assert config_path.exists()
    assert config_path.read_text(encoding="utf-8") == LOCAL_CONFIG_CONTENT
    assert "repo = " in LOCAL_CONFIG_CONTENT
    assert "use_gh_token" in LOCAL_CONFIG_CONTENT


def test_scaffolded_config_toml_is_inert(tmp_path: Path, capsys) -> None:
    """Every key is commented out, so a fresh scaffold changes nothing and warns about nothing."""
    get_ghtriage_dir(cwd=tmp_path, create=True)

    config = load_config(cwd=tmp_path)

    assert config.repo is None
    assert config.use_gh_token is False
    assert capsys.readouterr().err == ""


def test_get_ghtriage_dir_leaves_an_existing_config_toml_alone(tmp_path: Path) -> None:
    ghtriage_dir = tmp_path / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "config.toml").write_text('repo = "owner/repo"\n', encoding="utf-8")

    get_ghtriage_dir(cwd=tmp_path, create=True)

    assert (ghtriage_dir / "config.toml").read_text(encoding="utf-8") == 'repo = "owner/repo"\n'


def test_get_ghtriage_dir_writes_nothing_when_not_creating(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path, create=False)
    assert not ghtriage_dir.exists()


def test_load_config_reads_repo_and_use_gh_token(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text(
        'repo = "owner/from-config"\n\n[auth]\nuse_gh_token = true\n', encoding="utf-8"
    )

    config = load_config(cwd=tmp_path)

    assert config.repo == "owner/from-config"
    assert config.use_gh_token is True


def test_load_config_defaults_when_file_is_missing(tmp_path: Path) -> None:
    config = load_config(cwd=tmp_path)
    assert config.repo is None
    assert config.use_gh_token is False


def test_load_config_warns_about_an_unrecognized_key(tmp_path: Path, capsys) -> None:
    """A hand-edited file's typo would otherwise silently do nothing."""
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text('repoo = "owner/repo"\n', encoding="utf-8")

    config = load_config(cwd=tmp_path)

    err = capsys.readouterr().err
    assert config.repo is None
    assert "repoo" in err
    assert "config.toml" in err


def test_load_config_warns_about_an_unrecognized_key_in_a_known_table(
    tmp_path: Path, capsys
) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("[auth]\nuse_gh_tokens = true\n", encoding="utf-8")

    config = load_config(cwd=tmp_path)

    err = capsys.readouterr().err
    assert config.use_gh_token is False
    assert "auth.use_gh_tokens" in err


def test_load_config_warns_about_an_unrecognized_table(tmp_path: Path, capsys) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("[nope]\nkey = 1\n", encoding="utf-8")

    load_config(cwd=tmp_path)

    assert "nope" in capsys.readouterr().err


def test_load_config_raises_on_table_valued_repo(tmp_path: Path, capsys) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text('[repo]\nkey = "value"\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match="expected a string"):
        load_config(cwd=tmp_path)

    assert "unrecognized" not in capsys.readouterr().err


def test_load_config_raises_on_non_string_repo(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("repo = 3\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_config(cwd=tmp_path)


def test_load_config_raises_on_non_bool_use_gh_token(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text('[auth]\nuse_gh_token = "yes"\n', encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_config(cwd=tmp_path)


def test_load_config_raises_on_invalid_toml(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("[repo\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        load_config(cwd=tmp_path)


def test_resolve_token_prefers_environment_over_token_file(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "token").write_text("file-token\n", encoding="utf-8")

    token, source = resolve_token(cwd=tmp_path, env={"GITHUB_TOKEN": "env-token"})
    assert token == "env-token"
    assert source == "GITHUB_TOKEN (env)"


def test_resolve_token_reads_token_file(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "token").write_text("file-token\n", encoding="utf-8")

    token, source = resolve_token(cwd=tmp_path, env={})
    assert token == "file-token"
    assert "file" in source


def test_resolve_token_strips_whitespace_from_token_file(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "token").write_text("  file-token  \n", encoding="utf-8")

    token, _ = resolve_token(cwd=tmp_path, env={})
    assert token == "file-token"


def test_resolve_token_returns_not_configured_when_missing(tmp_path: Path) -> None:
    token, source = resolve_token(cwd=tmp_path, env={})
    assert token is None
    assert source == "not configured"


def _fake_gh(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0) -> None:
    """Stand in for the `gh auth token` subprocess without touching the real gh CLI."""

    def fake_run(cmd, **_kwargs):
        assert cmd == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    monkeypatch.setattr("ghtriage.config.subprocess.run", fake_run)


def _missing_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **_kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("ghtriage.config.subprocess.run", fake_run)


def _enable_gh_fallback(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("[auth]\nuse_gh_token = true\n", encoding="utf-8")


def test_resolve_token_ignores_gh_cli_when_fallback_is_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_ghtriage_dir(cwd=tmp_path)
    _fake_gh(monkeypatch, stdout="gh-token\n")

    token, source = resolve_token(cwd=tmp_path, env={})

    assert token is None
    assert source == "not configured"


def test_resolve_token_uses_gh_cli_when_fallback_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gh_fallback(tmp_path)
    _fake_gh(monkeypatch, stdout="gh-token\n")

    token, source = resolve_token(cwd=tmp_path, env={})

    assert token == "gh-token"
    assert "gh" in source


def test_resolve_token_prefers_the_token_file_over_the_gh_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gh_fallback(tmp_path)
    (tmp_path / ".ghtriage" / "token").write_text("file-token\n", encoding="utf-8")
    _fake_gh(monkeypatch, stdout="gh-token\n")

    token, source = resolve_token(cwd=tmp_path, env={})

    assert token == "file-token"
    assert "file" in source


def test_resolve_token_prefers_the_environment_over_the_gh_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gh_fallback(tmp_path)
    _fake_gh(monkeypatch, stdout="gh-token\n")

    token, source = resolve_token(cwd=tmp_path, env={"GITHUB_TOKEN": "env-token"})

    assert token == "env-token"
    assert source == "GITHUB_TOKEN (env)"


@pytest.mark.parametrize(
    "setup_gh",
    [
        _missing_gh,
        lambda mp: _fake_gh(mp, returncode=1),
        lambda mp: _fake_gh(mp, stdout="\n"),
    ],
    ids=["gh-not-installed", "gh-not-logged-in", "gh-returns-nothing"],
)
def test_resolve_token_falls_through_when_the_gh_cli_has_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup_gh
) -> None:
    _enable_gh_fallback(tmp_path)
    setup_gh(monkeypatch)

    token, source = resolve_token(cwd=tmp_path, env={})

    assert token is None
    assert source == "not configured"


def test_token_sources_reports_every_source_in_precedence_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gh_fallback(tmp_path)
    (tmp_path / ".ghtriage" / "token").write_text("file-token\n", encoding="utf-8")
    _fake_gh(monkeypatch, stdout="gh-token\n")

    sources = token_sources(cwd=tmp_path, env={})

    assert [source.name for source in sources] == [
        "GITHUB_TOKEN (env)",
        ".ghtriage/token",
        "gh auth token",
    ]
    assert [source.state for source in sources] == ["not set", "found", "available"]
    assert [source.token for source in sources] == [None, "file-token", "gh-token"]


def test_token_sources_marks_the_gh_source_disabled_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    get_ghtriage_dir(cwd=tmp_path)

    def explode(*_args, **_kwargs):
        raise AssertionError("gh should not run when the fallback is disabled")

    monkeypatch.setattr("ghtriage.config.subprocess.run", explode)

    gh_source = token_sources(cwd=tmp_path, env={})[-1]

    assert gh_source.state == "disabled"
    assert "ghtriage auth setup --use-gh-token" in gh_source.note


def test_token_sources_marks_the_gh_source_not_available_when_gh_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gh_fallback(tmp_path)
    _missing_gh(monkeypatch)

    gh_source = token_sources(cwd=tmp_path, env={})[-1]

    assert gh_source.state == "not available"
    assert gh_source.token is None


def test_gh_cli_token_distinguishes_missing_gh_from_a_logged_out_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _missing_gh(monkeypatch)
    assert "not installed" in (gh_cli_token().error or "")

    _fake_gh(monkeypatch, returncode=1)
    assert "gh auth login" in (gh_cli_token().error or "")

    _fake_gh(monkeypatch, stdout="gh-token\n")
    result = gh_cli_token()
    assert result.token == "gh-token"
    assert result.error is None


def test_enable_gh_token_fallback_preserves_comments_and_hand_edits(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    config_path = ghtriage_dir / "config.toml"
    config_path.write_text(
        '# my notes\nrepo = "owner/repo"\n\n[auth]\n# about auth\n', encoding="utf-8"
    )

    enable_gh_token_fallback(cwd=tmp_path)

    text = config_path.read_text(encoding="utf-8")
    assert "# my notes" in text
    assert "# about auth" in text
    config = load_config(cwd=tmp_path)
    assert config.repo == "owner/repo"
    assert config.use_gh_token is True


def test_enable_gh_token_fallback_works_on_a_fresh_scaffold(tmp_path: Path) -> None:
    enable_gh_token_fallback(cwd=tmp_path)

    text = (tmp_path / ".ghtriage" / "config.toml").read_text(encoding="utf-8")
    assert "# use_gh_token = true" in text
    assert load_config(cwd=tmp_path).use_gh_token is True


def test_resolve_repo_precedence_cli_over_config_over_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text('repo = "owner/from-config"\n', encoding="utf-8")

    monkeypatch.setattr(
        "ghtriage.config.get_git_remote_origin",
        lambda cwd=None: "git@github.com:owner/from-git.git",
    )

    assert resolve_repo(cli_repo="owner/from-cli", cwd=tmp_path) == "owner/from-cli"
    assert resolve_repo(cwd=tmp_path) == "owner/from-config"


def test_resolve_repo_falls_back_to_git_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ghtriage.config.get_git_remote_origin",
        lambda cwd=None: "https://github.com/owner/from-git.git",
    )
    assert resolve_repo(cwd=tmp_path) == "owner/from-git"


def test_resolve_repo_raises_on_invalid_config_toml(tmp_path: Path) -> None:
    ghtriage_dir = get_ghtriage_dir(cwd=tmp_path)
    (ghtriage_dir / "config.toml").write_text("[repo\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        resolve_repo(cwd=tmp_path)

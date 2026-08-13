import csv
import io
import json
from pathlib import Path
import re
import stat

import duckdb
import pytest

from ghtriage.cli import run
from ghtriage.config import GhTokenResult, load_config
from ghtriage.pipeline import SchemaGenerationMismatch
from ghtriage.query import execute_query


@pytest.fixture
def sample_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github.issues (id BIGINT, title VARCHAR, state VARCHAR)")
    con.execute("INSERT INTO github.issues VALUES (1, 'First', 'open'), (2, 'Second', 'closed')")
    con.close()

    return tmp_path


def test_query_table_format_success(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, title FROM issues ORDER BY id", "--format", "table"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "id" in captured.out
    assert "title" in captured.out
    assert "First" in captured.out
    assert captured.err == ""


def test_query_csv_format_success(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, title FROM issues ORDER BY id", "--format", "csv"])

    captured = capsys.readouterr()
    rows = list(csv.reader(io.StringIO(captured.out)))

    assert rc == 0
    assert rows[0] == ["id", "title"]
    assert rows[1] == ["1", "First"]
    assert rows[2] == ["2", "Second"]
    assert captured.err == ""
    assert "\r\n" not in captured.out


def test_query_json_format_is_strict_jsonl(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELECT id, state FROM issues ORDER BY id", "--format", "json"])

    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    payloads = [json.loads(line) for line in lines]

    assert rc == 0
    assert payloads == [{"id": 1, "state": "open"}, {"id": 2, "state": "closed"}]
    assert captured.err == ""


def test_query_returns_runtime_error_for_missing_db(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)

    rc = run(["query", "SELECT 1"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Database not found" in captured.err


def test_query_returns_runtime_error_for_bad_sql(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "SELEC id FROM issues"])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err


def test_query_rejects_write_sql_in_read_only_mode(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["query", "CREATE TABLE write_probe (id BIGINT)"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Query failed" in captured.err


def test_pull_points_at_a_full_refresh_when_a_step_warned(tmp_path: Path, monkeypatch, capsys):
    """Every post-load failure has the same one-command recovery, so say it next to them."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")
    monkeypatch.setattr(
        "ghtriage.cli.run_pull",
        lambda **_kwargs: ("load info", ["derived objects failed: boom"]),
    )

    rc = run(["pull"])

    err = capsys.readouterr().err
    assert rc == 0
    assert "derived objects failed: boom" in err
    assert "ghtriage pull --full" in err


def test_pull_says_nothing_extra_when_no_step_warned(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")
    monkeypatch.setattr("ghtriage.cli.run_pull", lambda **_kwargs: ("load info", []))

    rc = run(["pull"])

    assert rc == 0
    assert capsys.readouterr().err == ""


def test_pull_reports_a_schema_generation_mismatch_and_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")

    def refuse(**_kwargs):
        raise SchemaGenerationMismatch(stored=0, current=1)

    monkeypatch.setattr("ghtriage.cli.run_pull", refuse)

    rc = run(["pull"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "generation 0" in err
    assert "generation 1" in err
    assert "ghtriage pull --full" in err


def test_pull_without_a_token_points_at_auth_setup(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")

    rc = run(["pull"])

    err = capsys.readouterr().err
    assert rc == 1
    assert "ghtriage auth setup" in err
    assert "GITHUB_TOKEN" in err
    assert ".ghtriage/token" in err


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.parametrize("status_code", [401, 404])
def test_pull_explains_an_http_401_or_404_from_github(
    tmp_path: Path, monkeypatch, capsys, status_code: int
) -> None:
    """`auth setup` validates nothing, so a bad token or a pending org approval lands here."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")

    def fail(**_kwargs):
        inner = RuntimeError(f"{status_code} Client Error")
        inner.response = _FakeResponse(status_code)
        try:
            raise inner
        except RuntimeError as exc:
            # dlt wraps the request failure a few frames up; the guidance has to survive that.
            raise RuntimeError("pipeline step failed") from exc

    monkeypatch.setattr("ghtriage.cli.run_pull", fail)

    rc = run(["pull"])

    err = capsys.readouterr().err
    assert rc == 1
    assert str(status_code) in err
    assert "org" in err
    assert "classic" in err
    assert "ghtriage auth setup" in err


def test_pull_lets_an_unrelated_failure_raise(tmp_path: Path, monkeypatch) -> None:
    """A loud error is an acceptable outcome; only the auth case gets a translation."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda cli_repo=None: "owner/repo")

    def fail(**_kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("ghtriage.cli.run_pull", fail)

    with pytest.raises(RuntimeError, match="disk on fire"):
        run(["pull"])


def test_schema_lists_user_tables(sample_cwd: Path, monkeypatch, capsys) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE TABLE github._dlt_loads (load_id VARCHAR)")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out.splitlines()
    assert "_dlt_loads" not in captured.out


def test_schema_unknown_table_returns_runtime_error(sample_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "missing"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "Table not found" in captured.err


def test_schema_table_details_shows_description_column_when_comments_present(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("COMMENT ON COLUMN github.issues.title IS 'Title of the issue.'")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "issues"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "description" in captured.out
    assert "Title of the issue." in captured.out


def test_schema_table_details_omits_description_column_when_no_comments(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema", "--table", "issues"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "description" not in captured.out
    assert "id" in captured.out
    assert "BIGINT" in captured.out


def test_schema_listing_shows_table_descriptions_when_present(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("COMMENT ON TABLE github.issues IS 'Issues track tasks and bugs.'")
    con.close()

    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out
    assert "Issues track tasks and bugs." in captured.out


def test_schema_listing_plain_when_no_table_descriptions(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "issues" in captured.out.splitlines()


@pytest.fixture
def status_cwd(tmp_path: Path) -> Path:
    db_path = tmp_path / ".ghtriage" / "ghtriage.duckdb"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA github")
    con.execute("CREATE TABLE github._ghtriage_meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('repo', 'owner/repo')")
    con.execute(
        "INSERT INTO github._ghtriage_meta VALUES ('last_pull_at', '2026-02-28T14:22:57Z')"
    )
    con.execute("INSERT INTO github._ghtriage_meta VALUES ('last_full_pull', 'false')")
    con.execute("CREATE TABLE github.issues (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.pull_requests (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.conversation_comments (id BIGINT, updated_at TIMESTAMP)")
    con.execute("CREATE TABLE github.review_comments (id BIGINT, updated_at TIMESTAMP)")
    con.close()

    return tmp_path


def test_status_shows_db_info(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "owner/repo" in captured.out
    assert "2026-02-28" in captured.out
    assert captured.err == ""


def test_status_not_yet_pulled_without_db(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "not yet pulled" in captured.out
    assert captured.err == ""


def test_status_shows_mismatch_warning(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/other-repo")

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "WARNING" in captured.out
    assert "owner/other-repo" in captured.out
    assert "owner/repo" in captured.out


def test_status_handles_missing_config_repo(status_cwd: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(status_cwd)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setattr(
        "ghtriage.cli.resolve_repo", lambda: (_ for _ in ()).throw(RuntimeError("no remote"))
    )

    rc = run(["status"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "unknown" in captured.out


@pytest.fixture
def auth_cwd(tmp_path: Path, monkeypatch) -> Path:
    """A working directory where repo resolution succeeds and nothing else is set up."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr("ghtriage.cli.resolve_repo", lambda: "owner/repo")
    return tmp_path


def _answer(monkeypatch, *, choice: str | None = None, token: str | None = None) -> None:
    if choice is not None:
        monkeypatch.setattr("builtins.input", lambda _prompt="": choice)
    if token is not None:
        monkeypatch.setattr("ghtriage.cli.getpass", lambda _prompt="": token)


def test_auth_setup_creates_the_directory_and_scaffolding_eagerly(auth_cwd, monkeypatch, capsys):
    """Aborting at the paste prompt still leaves a usable, hand-editable directory."""
    _answer(monkeypatch, choice="1")
    monkeypatch.setattr(
        "ghtriage.cli.getpass", lambda _prompt="": (_ for _ in ()).throw(EOFError())
    )

    rc = run(["auth", "setup"])

    capsys.readouterr()
    assert rc == 1
    assert (auth_cwd / ".ghtriage" / ".gitignore").exists()
    assert (auth_cwd / ".ghtriage" / "config.toml").exists()
    assert not (auth_cwd / ".ghtriage" / "token").exists()


def test_auth_setup_menu_defaults_to_the_fine_grained_token(auth_cwd, monkeypatch, capsys):
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", lambda prompt="": (prompts.append(prompt), "")[1])
    _answer(monkeypatch, token="ghp_pasted")

    rc = run(["auth", "setup"])

    out = capsys.readouterr().out
    assert rc == 0
    assert prompts == ["Choice [1]: "]
    assert "1. Fine-grained personal access token" in out
    assert "https://github.com/settings/personal-access-tokens/new?" in out


def test_auth_setup_fine_grained_link_carries_the_read_only_permissions(
    auth_cwd, monkeypatch, capsys
):
    _answer(monkeypatch, choice="1", token="ghp_pasted")

    run(["auth", "setup"])

    out = capsys.readouterr().out
    assert (
        "https://github.com/settings/personal-access-tokens/new"
        "?name=ghtriage+(owner/repo)&description=Read+issues+and+PRs+for+ghtriage"
        "&target_name=owner&issues=read&pull_requests=read" in out
    )
    # The URL cannot select the repository or the owner, so the instructions must.
    assert "Only select repositories" in out
    assert "owner/repo" in out


def test_auth_setup_classic_link_carries_the_repo_scope_and_its_tradeoff(
    auth_cwd, monkeypatch, capsys
):
    _answer(monkeypatch, choice="2", token="ghp_pasted")

    run(["auth", "setup"])

    out = capsys.readouterr().out
    assert (
        "https://github.com/settings/tokens/new?scopes=repo&description=ghtriage+(owner/repo)"
        in out
    )
    assert "write" in out


def test_auth_setup_falls_back_to_generic_links_when_the_repo_is_unknown(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        "ghtriage.cli.resolve_repo", lambda: (_ for _ in ()).throw(RuntimeError("no remote"))
    )
    _answer(monkeypatch, choice="1", token="ghp_pasted")

    rc = run(["auth", "setup"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "https://github.com/settings/personal-access-tokens/new?name=ghtriage&" in out
    assert "target_name" not in out


def test_auth_setup_saves_the_pasted_token_stripped_and_private(auth_cwd, monkeypatch, capsys):
    _answer(monkeypatch, choice="1", token="  ghp_pasted \n")

    rc = run(["auth", "setup"])

    capsys.readouterr()
    token_path = auth_cwd / ".ghtriage" / "token"
    assert rc == 0
    assert token_path.read_text(encoding="utf-8").strip() == "ghp_pasted"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_auth_setup_rejects_an_empty_paste(auth_cwd, monkeypatch, capsys):
    _answer(monkeypatch, choice="1", token="   ")

    rc = run(["auth", "setup"])

    assert rc == 1
    assert "No token" in capsys.readouterr().err
    assert not (auth_cwd / ".ghtriage" / "token").exists()


def test_auth_setup_warns_that_the_environment_wins_over_the_saved_file(
    auth_cwd, monkeypatch, capsys
):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    _answer(monkeypatch, choice="1", token="ghp_pasted")

    run(["auth", "setup"])

    captured = capsys.readouterr()
    assert "GITHUB_TOKEN" in captured.out + captured.err
    assert "precedence" in captured.out + captured.err


def test_auth_setup_says_when_an_existing_token_file_will_be_replaced(
    auth_cwd, monkeypatch, capsys
):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "token").write_text("old-token\n", encoding="utf-8")
    _answer(monkeypatch, choice="1", token="ghp_pasted")

    run(["auth", "setup"])

    out = capsys.readouterr().out
    assert "already exists" in out
    assert (ghtriage_dir / "token").read_text(encoding="utf-8").strip() == "ghp_pasted"


def test_auth_setup_rejects_an_unrecognized_choice(auth_cwd, monkeypatch, capsys):
    _answer(monkeypatch, choice="9")

    rc = run(["auth", "setup"])

    assert rc == 1
    assert "9" in capsys.readouterr().err


def test_auth_setup_choice_three_enables_the_gh_fallback(auth_cwd, monkeypatch, capsys):
    _answer(monkeypatch, choice="3")
    monkeypatch.setattr("ghtriage.cli.gh_cli_token", lambda: GhTokenResult(token="gh-token"))

    rc = run(["auth", "setup"])

    out = capsys.readouterr().out
    assert rc == 0
    assert load_config(cwd=auth_cwd).use_gh_token is True
    assert "gh" in out


def test_auth_setup_use_gh_token_flag_skips_the_menu(auth_cwd, monkeypatch, capsys):
    def no_prompt(_prompt=""):
        raise AssertionError("--use-gh-token must not prompt")

    monkeypatch.setattr("builtins.input", no_prompt)
    monkeypatch.setattr("ghtriage.cli.gh_cli_token", lambda: GhTokenResult(token="gh-token"))

    rc = run(["auth", "setup", "--use-gh-token"])

    capsys.readouterr()
    assert rc == 0
    assert load_config(cwd=auth_cwd).use_gh_token is True


def test_auth_setup_gh_fallback_preserves_hand_edits(auth_cwd, monkeypatch, capsys):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "config.toml").write_text(
        '# hand written\nrepo = "owner/other"\n', encoding="utf-8"
    )
    monkeypatch.setattr("ghtriage.cli.gh_cli_token", lambda: GhTokenResult(token="gh-token"))

    run(["auth", "setup", "--use-gh-token"])

    capsys.readouterr()
    text = (ghtriage_dir / "config.toml").read_text(encoding="utf-8")
    assert "# hand written" in text
    assert load_config(cwd=auth_cwd).repo == "owner/other"
    assert load_config(cwd=auth_cwd).use_gh_token is True


def test_auth_setup_writes_the_setting_even_when_gh_is_unusable(auth_cwd, monkeypatch, capsys):
    """The user may install or log into gh afterward; the preference is theirs either way."""
    monkeypatch.setattr(
        "ghtriage.cli.gh_cli_token",
        lambda: GhTokenResult(error="gh CLI is not logged in (run `gh auth login`)"),
    )

    rc = run(["auth", "setup", "--use-gh-token"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "gh auth login" in captured.out + captured.err
    assert load_config(cwd=auth_cwd).use_gh_token is True


def test_auth_status_lists_every_source_and_marks_the_one_in_use(auth_cwd, monkeypatch, capsys):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "token").write_text("file-token\n", encoding="utf-8")

    rc = run(["auth", "status"])

    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.strip()]
    assert rc == 0
    assert lines[0].startswith("GITHUB_TOKEN (env)")
    assert "not set" in lines[0]
    assert lines[1].startswith(".ghtriage/token")
    assert "found" in lines[1]
    assert "<- in use" in lines[1]
    assert lines[2].startswith("gh auth token")
    assert "disabled" in lines[2]
    assert "ghtriage auth setup --use-gh-token" in lines[2]


def test_auth_status_arrow_follows_precedence(auth_cwd, monkeypatch, capsys):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "token").write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")

    run(["auth", "status"])

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "<- in use" in lines[0]
    assert "<- in use" not in lines[1]


def test_auth_status_reports_the_gh_source_when_the_fallback_is_enabled(
    auth_cwd, monkeypatch, capsys
):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "config.toml").write_text("[auth]\nuse_gh_token = true\n", encoding="utf-8")
    monkeypatch.setattr("ghtriage.config.gh_cli_token", lambda: GhTokenResult(token="gh-token"))

    run(["auth", "status"])

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "available" in lines[2]
    assert "<- in use" in lines[2]


def test_auth_status_reports_an_enabled_but_unusable_gh(auth_cwd, monkeypatch, capsys):
    ghtriage_dir = auth_cwd / ".ghtriage"
    ghtriage_dir.mkdir()
    (ghtriage_dir / "config.toml").write_text("[auth]\nuse_gh_token = true\n", encoding="utf-8")
    monkeypatch.setattr(
        "ghtriage.config.gh_cli_token", lambda: GhTokenResult(error="gh CLI is not installed")
    )

    run(["auth", "status"])

    out = capsys.readouterr().out
    assert "not available" in out
    assert "not installed" in out
    assert "<- in use" not in out


def test_auth_status_points_at_setup_when_nothing_is_configured(auth_cwd, monkeypatch, capsys):
    rc = run(["auth", "status"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "ghtriage auth setup" in out


def test_auth_requires_a_subcommand(auth_cwd, monkeypatch):
    with pytest.raises(SystemExit) as exc_info:
        run(["auth"])
    assert exc_info.value.code == 2


@pytest.fixture
def cwd_with_view(sample_cwd: Path) -> Path:
    """sample_cwd plus a documented derived view, as create_views() would leave it."""
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE VIEW github.issue_activity AS SELECT id, title FROM github.issues")
        con.execute("COMMENT ON VIEW github.issue_activity IS 'Derived view: one row per issue.'")
        con.execute("COMMENT ON COLUMN github.issue_activity.id IS 'Pass-through of issues.id.'")
    return sample_cwd


def test_schema_listing_shows_view_description(cwd_with_view: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_view)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "issue_activity" in out
    assert "Derived view: one row per issue." in out


def test_schema_table_details_works_on_a_view(cwd_with_view: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_view)

    rc = run(["schema", "--table", "issue_activity"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Pass-through of issues.id." in out


@pytest.fixture
def cwd_with_index(sample_cwd: Path) -> Path:
    """sample_cwd plus a full-text index, as create_search_indexes() would leave it.

    Over the declared columns: an index that cannot cover them all is dropped instead,
    which is what lets `schema` report the declaration.
    """
    db_path = sample_cwd / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("ALTER TABLE github.issues ADD COLUMN body VARCHAR")
        con.execute("PRAGMA create_fts_index('github.issues', 'id', 'title', 'body', overwrite=1)")
    return sample_cwd


def test_schema_listing_shows_full_text_indexes(cwd_with_index: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_index)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Full-text search indexes" in out
    assert "fts_github_issues.match_bm25" in out
    # The index's row: key and column set from the declaration, count from the table.
    # Matched together so neither can be satisfied by text elsewhere in the output.
    assert re.search(r"issues\s*\|\s*id\s*\|\s*title, body\s*\|\s*2\b", out)


def test_schema_listing_omits_index_block_when_there_are_none(
    sample_cwd: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(sample_cwd)

    rc = run(["schema"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Full-text search" not in out


def test_schema_listing_says_where_the_macro_can_be_called_from(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    """That the macro travels with the id is what makes it worth surfacing at all."""
    monkeypatch.chdir(cwd_with_index)

    run(["schema"])

    out = capsys.readouterr().out
    assert "not bound to the indexed table" in out
    assert "An id the index does not hold scores NULL." in out


def test_schema_table_details_shows_the_index(cwd_with_index: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(cwd_with_index)

    rc = run(["schema", "--table", "issues"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "fts_github_issues.match_bm25(id, 'query') over title, body (2 documents)" in out


def test_schema_table_details_omits_index_line_for_unindexed_table(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    db_path = cwd_with_index / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE github.other (id BIGINT)")
    monkeypatch.chdir(cwd_with_index)

    run(["schema", "--table", "other"])

    assert "match_bm25" not in capsys.readouterr().out


def test_schema_index_example_uses_the_first_declared_table(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    """Declaration order, so the example is `issues` rather than a comment table."""
    db_path = cwd_with_index / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("CREATE TABLE github.conversation_comments (id BIGINT, body VARCHAR)")
        con.execute("INSERT INTO github.conversation_comments VALUES (1, 'a comment')")
        con.execute(
            "PRAGMA create_fts_index('github.conversation_comments', 'id', 'body', overwrite=1)"
        )
    monkeypatch.chdir(cwd_with_index)

    run(["schema"])

    out = capsys.readouterr().out
    assert "SELECT number, title, score FROM (" in out
    assert "fts_github_issues.match_bm25(id, 'search terms') AS score FROM issues" in out


def test_schema_index_example_executes_as_printed(
    cwd_with_index: Path, monkeypatch, capsys
) -> None:
    """The example is guidance agents copy verbatim, so the suite runs it verbatim --
    lifted from the actual output, with only the search terms substituted."""
    db_path = cwd_with_index / ".ghtriage" / "ghtriage.duckdb"
    with duckdb.connect(str(db_path)) as con:
        con.execute("ALTER TABLE github.issues ADD COLUMN number BIGINT")
        con.execute("UPDATE github.issues SET number = id")
        con.execute("UPDATE github.issues SET body = 'segfault on startup' WHERE id = 1")
        con.execute("PRAGMA create_fts_index('github.issues', 'id', 'title', 'body', overwrite=1)")
    monkeypatch.chdir(cwd_with_index)

    run(["schema"])

    lines = capsys.readouterr().out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("Search a table"))
    example = "\n".join(lines[start + 1 : start + 4])
    # Through execute_query, the path a copied example actually takes in `ghtriage query`.
    columns, results = execute_query(example.replace("'search terms'", "'segfault'"))

    assert columns == ["number", "title", "score"]
    assert len(results) == 1
    number, title, score = results[0]
    assert (number, title) == (1, "First")
    assert score is not None

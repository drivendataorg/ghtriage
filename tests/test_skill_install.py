import importlib.metadata
import importlib.resources
from pathlib import Path

import pytest

from ghtriage.skill_install import (
    SkillInstallError,
    _decode_skill_text,
    _stamp_version,
    install_skill,
    resolve_destination,
)

SOURCE_SKILL_DIR = Path(__file__).resolve().parents[1] / "src" / "ghtriage" / "skills" / "ghtriage"


def _frontmatter_lines(text: str) -> list[str]:
    lines = text.split("\n")
    assert lines[0] == "---", "SKILL.md must open with YAML frontmatter"
    end = lines.index("---", 1)
    return lines[1:end]


def _source_tree() -> dict[str, bytes]:
    return {
        path.relative_to(SOURCE_SKILL_DIR).as_posix(): path.read_bytes()
        for path in sorted(SOURCE_SKILL_DIR.rglob("*"))
        if path.is_file()
    }


def _packaged_tree(traversable, prefix: str = "") -> dict[str, bytes]:
    tree: dict[str, bytes] = {}
    for entry in traversable.iterdir():
        name = f"{prefix}{entry.name}"
        if entry.is_dir():
            tree.update(_packaged_tree(entry, prefix=f"{name}/"))
        else:
            tree[name] = entry.read_bytes()
    return tree


def test_source_skill_frontmatter_is_marked_and_unversioned() -> None:
    """The shape the line-based version stamper depends on.

    The source carries `managed-by: ghtriage` (the ownership marker both distribution
    channels ship) and deliberately no `version:` key -- see the "Skill distribution"
    entry in docs/decisions.md. The version is inserted into the installed copy.
    """
    frontmatter = _frontmatter_lines((SOURCE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))

    assert "metadata:" in frontmatter
    assert "  managed-by: ghtriage" in frontmatter
    assert not any(line.strip().startswith("version:") for line in frontmatter)


def test_skill_files_are_packaged_as_package_data() -> None:
    """`uv_build` includes non-Python files under the module root -- pinned, not assumed.

    Under plain `uv run pytest` the install is editable, so importlib.resources resolves
    to the source tree and this compares it to itself. The test only has teeth under
    `just test`, which installs the built wheel non-editable -- keep running that.
    """
    packaged = _packaged_tree(importlib.resources.files("ghtriage") / "skills" / "ghtriage")
    source = _source_tree()

    assert source, "the source skill tree is empty"
    assert packaged == source


def test_skill_ships_under_a_directory_named_skills() -> None:
    """`gh skill install` discovers only `skills/*/SKILL.md` layouts; the parent
    directory's name is the invariant the verbatim distribution channel depends on."""
    assert SOURCE_SKILL_DIR.parent.name == "skills"
    assert (importlib.resources.files("ghtriage") / "skills" / "ghtriage" / "SKILL.md").is_file()


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A home directory of our own, so no test can read or write the real one."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home_dir))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return home_dir


def test_resolve_claude_project_scope(tmp_path: Path, home: Path) -> None:
    destination = resolve_destination(agent="claude-code", scope="project", cwd=tmp_path)

    assert destination == tmp_path / ".claude" / "skills" / "ghtriage"


def test_resolve_universal_project_scope(tmp_path: Path, home: Path) -> None:
    destination = resolve_destination(agent="universal", scope="project", cwd=tmp_path)

    assert destination == tmp_path / ".agents" / "skills" / "ghtriage"


def test_resolve_universal_user_scope(tmp_path: Path, home: Path) -> None:
    destination = resolve_destination(agent="universal", scope="user", cwd=tmp_path)

    assert destination == home / ".agents" / "skills" / "ghtriage"


def test_resolve_claude_user_scope_honors_claude_config_dir(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    """Claude Code moves skills discovery with its config dir; follow it, don't hardcode."""
    relocated = tmp_path / "relocated"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(relocated))

    destination = resolve_destination(agent="claude-code", scope="user", cwd=tmp_path)

    assert destination == relocated / "skills" / "ghtriage"
    assert capsys.readouterr().err == ""


def test_resolve_claude_user_scope_falls_back_to_dot_claude(
    tmp_path: Path, home: Path, capsys
) -> None:
    destination = resolve_destination(agent="claude-code", scope="user", cwd=tmp_path)

    assert destination == home / ".claude" / "skills" / "ghtriage"
    assert capsys.readouterr().err == ""


def test_resolve_claude_user_scope_notes_a_relocated_looking_config_dir(
    tmp_path: Path, home: Path, capsys
) -> None:
    """A desktop-launched Claude Code can carry CLAUDE_CONFIG_DIR when this shell does not,
    so an existing ~/.config/claude is worth naming -- as a note, not a guess. The probe is
    the config directory itself: a relocated Claude Code that has never installed a skill
    has no skills/ child yet, and that user needs the note most."""
    (home / ".config" / "claude").mkdir(parents=True)

    destination = resolve_destination(agent="claude-code", scope="user", cwd=tmp_path)

    err = capsys.readouterr().err
    assert destination == home / ".claude" / "skills" / "ghtriage"
    assert str(home / ".claude" / "skills") in err
    assert str(home / ".config" / "claude") in err
    assert "CLAUDE_CONFIG_DIR" in err
    assert "--dir" in err


def test_resolve_explicit_directory(tmp_path: Path, home: Path) -> None:
    destination = resolve_destination(agent=None, scope=None, directory=tmp_path / "anywhere")

    assert destination == tmp_path / "anywhere" / "ghtriage"


@pytest.mark.parametrize("scope", [None, "project", "user"])
def test_resolve_without_an_agent_or_directory_errors(
    scope: str | None, tmp_path: Path, home: Path
) -> None:
    """`directory` is the only reason an agent may be missing, so every other path is an
    error rather than a default: the universal directory would be a silent wrong install."""
    with pytest.raises(SkillInstallError):
        resolve_destination(agent=None, scope=scope, cwd=tmp_path)


def _installed_frontmatter(destination: Path) -> list[str]:
    return _frontmatter_lines((destination / "SKILL.md").read_text(encoding="utf-8"))


def _fake_managed_install(destination: Path, version: str) -> None:
    """A previous install of another version, with a reference file the source no longer has."""
    (destination / "references").mkdir(parents=True)
    (destination / "SKILL.md").write_text(
        "---\nname: ghtriage\nmetadata:\n  managed-by: ghtriage\n"
        f"  version: {version}\n---\n\nold\n",
        encoding="utf-8",
    )
    (destination / "references" / "stale.md").write_text("gone next time\n", encoding="utf-8")


def test_install_creates_the_tree_and_stamps_the_version(tmp_path: Path) -> None:
    destination = tmp_path / "skills" / "ghtriage"

    result = install_skill(destination)

    frontmatter = _installed_frontmatter(destination)
    assert result.action == "installed"
    assert result.destination == destination
    assert result.previous_version is None
    assert result.version == importlib.metadata.version("ghtriage")
    assert "  managed-by: ghtriage" in frontmatter
    assert f"  version: {result.version}" in frontmatter
    assert (destination / "references" / "query-cookbook.md").read_bytes() == _source_tree()[
        "references/query-cookbook.md"
    ]


def test_reinstalling_identical_content_is_a_no_op(tmp_path: Path) -> None:
    destination = tmp_path / "ghtriage"
    install_skill(destination)
    before = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in destination.rglob("*")
        if path.is_file()
    }

    result = install_skill(destination)

    after = {
        path: (path.stat().st_mtime_ns, path.read_bytes())
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert result.action == "unchanged"
    assert result.previous_version == result.version
    assert after == before


def test_install_over_a_managed_skill_replaces_the_whole_directory(tmp_path: Path) -> None:
    destination = tmp_path / "ghtriage"
    _fake_managed_install(destination, "0.0.1")

    result = install_skill(destination)

    assert result.action == "replaced"
    assert result.previous_version == "0.0.1"
    assert result.version == importlib.metadata.version("ghtriage")
    assert not (destination / "references" / "stale.md").exists()
    assert "  managed-by: ghtriage" in _installed_frontmatter(destination)


def test_install_refuses_an_unmarked_destination(tmp_path: Path) -> None:
    destination = tmp_path / "ghtriage"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("---\nname: ghtriage\n---\n\nhand written\n", "utf-8")

    with pytest.raises(SkillInstallError) as exc_info:
        install_skill(destination)

    assert str(destination) in str(exc_info.value)
    assert "--force" in str(exc_info.value)
    assert (destination / "SKILL.md").read_text(encoding="utf-8").endswith("hand written\n")


def test_force_replaces_an_unmarked_destination(tmp_path: Path) -> None:
    destination = tmp_path / "ghtriage"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("---\nname: ghtriage\n---\n\nhand written\n", "utf-8")

    result = install_skill(destination, force=True)

    assert result.action == "replaced"
    assert result.previous_version is None
    assert "  managed-by: ghtriage" in _installed_frontmatter(destination)


def test_install_over_an_unversioned_managed_skill_replaces_it(tmp_path: Path) -> None:
    """What `gh skill install` leaves behind: our marker, no version stamp. Still ours."""
    destination = tmp_path / "ghtriage"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text(
        (SOURCE_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"), encoding="utf-8"
    )

    result = install_skill(destination)

    assert result.action == "replaced"
    assert result.previous_version is None
    assert f"  version: {result.version}" in _installed_frontmatter(destination)


def test_install_refuses_a_directory_with_no_skill_file(tmp_path: Path) -> None:
    destination = tmp_path / "ghtriage"
    (destination / "notes").mkdir(parents=True)

    with pytest.raises(SkillInstallError):
        install_skill(destination)

    assert (destination / "notes").exists()


def test_install_refuses_a_skill_file_without_frontmatter(tmp_path: Path) -> None:
    """No frontmatter means no marker to read: unowned, and refused rather than parsed."""
    destination = tmp_path / "ghtriage"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("# hand written\n", encoding="utf-8")

    with pytest.raises(SkillInstallError):
        install_skill(destination)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "# hand written\n"


def test_stamper_handles_crlf_source_bytes() -> None:
    """A wheel built from a Windows checkout (no .gitattributes, autocrlf) carries CRLF
    SKILL.md bytes; the line-based stamper must not choke on them."""
    # Canonicalize to LF first: on a CRLF checkout (Windows CI) the raw bytes already
    # carry \r\n, and a bare \n -> \r\n replacement would manufacture \r\r\n.
    source = (SOURCE_SKILL_DIR / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")
    crlf = source.replace(b"\n", b"\r\n")

    stamped = _stamp_version(_decode_skill_text(crlf), "1.2.3")

    assert "  version: 1.2.3" in stamped.split("\n")


def test_interrupted_install_is_repaired_by_the_next_run(tmp_path: Path, monkeypatch) -> None:
    """SKILL.md is written first, so a torn install keeps the managed-by marker and the
    next run replaces it, rather than refusing as an unmarked directory."""
    destination = tmp_path / "ghtriage"
    original_write_bytes = Path.write_bytes
    writes: list[Path] = []

    def failing_write_bytes(self: Path, data: bytes) -> int:
        writes.append(self)
        if len(writes) == 2:
            raise OSError("no space left on device")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", failing_write_bytes)
    with pytest.raises(OSError):
        install_skill(destination)

    assert (destination / "SKILL.md").is_file()
    result = install_skill(destination)
    assert result.action == "replaced"


def test_install_over_a_gh_installed_copy_replaces_it(tmp_path: Path) -> None:
    """`gh skill install` re-dumps the frontmatter (keys sorted, 4-space indent, an added
    local-path); the marker must still read as ours or the documented cross-channel
    upgrade path refuses instead."""
    destination = tmp_path / "ghtriage"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text(
        "---\ndescription: whatever\nmetadata:\n    local-path: /tmp/gone\n"
        "    managed-by: ghtriage\nname: ghtriage\n---\n\nbody\n",
        encoding="utf-8",
    )

    result = install_skill(destination)

    assert result.action == "replaced"
    assert result.previous_version is None

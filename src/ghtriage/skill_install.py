from dataclasses import dataclass
import importlib.metadata
import importlib.resources
import os
from pathlib import Path
import shutil
import sys

try:
    from importlib.resources.abc import Traversable
except ModuleNotFoundError:  # pragma: no cover - exercised on Python <3.11
    from importlib.abc import Traversable

SKILL_NAME = "ghtriage"
SKILL_FILE = "SKILL.md"
MANAGED_BY_MARKER = "managed-by: ghtriage"

PROJECT_SKILLS_DIRS = {
    "claude-code": Path(".claude") / "skills",
    "universal": Path(".agents") / "skills",
}


def _claude_user_skills_dir() -> Path:
    """Where a Claude Code launched from this shell would look for its personal skills."""
    # CLAUDE_CONFIG_DIR relocates Claude Code's whole config directory, personal skills
    # included, so honoring it before ~/.claude reproduces Claude Code's own lookup rules
    # in this environment.
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "skills"

    fallback = Path.home() / ".claude" / "skills"
    relocated = Path.home() / ".config" / "claude"
    if relocated.is_dir():
        # The config directory, not its skills/ child: a relocated Claude Code that has
        # never installed a skill has no skills/ yet, and that user needs this note most.
        # A desktop-launched Claude Code can carry CLAUDE_CONFIG_DIR when this shell does
        # not, which is unknowable from here -- so say what was chosen, not what to do.
        print(
            f"Note: installing to {fallback} because CLAUDE_CONFIG_DIR is unset, but "
            f"{relocated} also exists. If that is the config directory Claude Code uses, "
            "set CLAUDE_CONFIG_DIR or pass --dir.",
            file=sys.stderr,
        )
    return fallback


def _user_skills_dir(agent: str) -> Path:
    if agent == "claude-code":
        return _claude_user_skills_dir()
    return Path.home() / ".agents" / "skills"


def resolve_destination(
    agent: str | None,
    scope: str | None = None,
    directory: str | Path | None = None,
    cwd: str | Path | None = None,
) -> Path:
    """The skill directory to install into. `directory` is the escape hatch and wins."""
    if directory is not None:
        return Path(directory) / SKILL_NAME
    if scope == "user":
        return _user_skills_dir(agent) / SKILL_NAME
    root = Path(cwd) if cwd is not None else Path.cwd()
    return root / PROJECT_SKILLS_DIRS[agent] / SKILL_NAME


class SkillInstallError(RuntimeError):
    """An install the command refuses. The CLI reports these as messages, not tracebacks."""


@dataclass(frozen=True)
class InstallResult:
    """What the install did, with everything the CLI's one output line needs."""

    destination: Path
    action: str
    version: str
    previous_version: str | None = None


def skill_version() -> str:
    return importlib.metadata.version("ghtriage")


def _skill_source() -> Traversable:
    # The parent directory must be named `skills`: `gh skill install` discovers only
    # `skills/*/SKILL.md`-shaped layouts, and the verbatim channel depends on it.
    return importlib.resources.files("ghtriage") / "skills" / SKILL_NAME


def _frontmatter_bounds(lines: list[str]) -> tuple[int, int]:
    return 1, lines.index("---", 1)


def _stamp_version(text: str, version: str) -> str:
    """Insert `version:` into the copied SKILL.md's `metadata:` block, line by line.

    The source frontmatter is ours and its shape is pinned by a test, so this stays a
    line edit rather than a YAML round-trip that would reflow the hand-written blocks.
    """
    lines = text.split("\n")
    start, end = _frontmatter_bounds(lines)
    metadata_index = lines.index("metadata:", start, end)
    insert_at = metadata_index + 1
    while insert_at < end and lines[insert_at].startswith((" ", "\t")):
        insert_at += 1
    lines.insert(insert_at, f"  version: {version}")
    return "\n".join(lines)


def _decode_skill_text(data: bytes) -> str:
    # The wheel carries whatever line endings the build checkout had (a Windows clone
    # without .gitattributes commits CRLF); the line-based stamper needs LF.
    return data.decode("utf-8").replace("\r\n", "\n")


def _render_skill(version: str) -> dict[str, bytes]:
    """The exact bytes to write, keyed by path relative to the skill directory."""
    files: dict[str, bytes] = {}
    _collect(_skill_source(), "", files)
    files[SKILL_FILE] = _stamp_version(_decode_skill_text(files[SKILL_FILE]), version).encode(
        "utf-8"
    )
    return files


def _collect(traversable: Traversable, prefix: str, files: dict[str, bytes]) -> None:
    for entry in traversable.iterdir():
        name = f"{prefix}{entry.name}"
        if entry.is_dir():
            _collect(entry, f"{name}/", files)
        else:
            files[name] = entry.read_bytes()


def _existing_files(destination: Path) -> dict[str, bytes]:
    return {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }


def _installed_version(skill_text: str) -> str | None:
    lines = skill_text.split("\n")
    start, end = _frontmatter_bounds(lines)
    for line in lines[start:end]:
        if line.strip().startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def _is_managed(skill_text: str) -> bool:
    lines = skill_text.split("\n")
    start, end = _frontmatter_bounds(lines)
    return any(line.strip() == MANAGED_BY_MARKER for line in lines[start:end])


def _read_existing_skill(destination: Path) -> str | None:
    """The installed SKILL.md, or None when there is nothing recognizable to read."""
    skill_path = destination / SKILL_FILE
    if not skill_path.is_file():
        return None
    text = skill_path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines[0] != "---" or "---" not in lines[1:]:
        return None
    return text


def install_skill(destination: Path, force: bool = False) -> InstallResult:
    version = skill_version()
    files = _render_skill(version)

    if not destination.exists():
        _write(destination, files)
        return InstallResult(destination=destination, action="installed", version=version)

    existing_skill = _read_existing_skill(destination)
    managed = existing_skill is not None and _is_managed(existing_skill)
    if not managed and not force:
        raise SkillInstallError(
            f"{destination} already exists and was not installed by ghtriage.\n"
            "Pass --force to replace it."
        )

    # A destination ghtriage marked as its own is replaced without --force: requiring the
    # flag for every routine upgrade would train users to always pass it, which is exactly
    # what would gut the guard above for a skill directory ghtriage does not own.
    previous_version = _installed_version(existing_skill) if managed else None
    if managed and _existing_files(destination) == files:
        return InstallResult(
            destination=destination,
            action="unchanged",
            version=version,
            previous_version=previous_version,
        )

    shutil.rmtree(destination)
    _write(destination, files)
    return InstallResult(
        destination=destination,
        action="replaced",
        version=version,
        previous_version=previous_version,
    )


def _write(destination: Path, files: dict[str, bytes]) -> None:
    # SKILL.md first: an interrupted install then always leaves the managed-by marker
    # behind, so the next run repairs it through the managed-replace branch instead of
    # refusing as an unmarked directory.
    for relative_path in sorted(files, key=lambda p: p != SKILL_FILE):
        path = destination / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[relative_path])

"""
Claude Code skills shipped with labctl, and installing them (#56).

The skills live in the package (``labctl/skills/<name>/SKILL.md``), so a
PyPI install has them. ``labctl skill install`` copies one to where Claude
Code loads it: ``<project>/.claude/skills/<name>/SKILL.md`` or
``~/.claude/skills/<name>/SKILL.md``.

Writes never follow a symlink below the project (or home) directory: the
``.claude/skills/<name>`` directories are opened one component at a time
with ``O_NOFOLLOW`` (labctl.core.safe_open), and the file is written to a
temporary name and renamed over ``SKILL.md``, which replaces a link rather
than writing through it.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional

from labctl.core.safe_open import open_dir_nofollow

SKILL_FILE = "SKILL.md"

# The skill reads per-target settings from this file in the project.
DEPLOY_CONFIG = Path(".claude") / "deploy-config.json"
DEPLOY_CONFIG_EXAMPLE = {
    "targets": {
        "my-board": {
            "build_command": "make",
            "binary": "build/kernel.img",
            "sd_dest": "kernel.img",
            "partition": 1,
            "serial_port": "my-board",
            "success_pattern": "login:",
            "boot_timeout": 30,
        }
    },
    "default_target": "my-board",
}


class SkillError(Exception):
    """An install or uninstall that can't be done (the message says why)."""


@dataclass(frozen=True)
class Outcome:
    """What ``install``/``uninstall`` did: ``status`` is ``installed``,
    ``updated``, ``unchanged``, ``removed`` or ``absent``."""

    status: str
    path: Path


def bundled() -> list[str]:
    """Names of the skills shipped with labctl."""
    root = resources.files("labctl").joinpath("skills")
    return sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and entry.joinpath(SKILL_FILE).is_file()
    )


def text(name: str) -> bytes:
    """The bundled ``SKILL.md`` of ``name`` (SkillError if unknown)."""
    if name not in bundled():
        known = ", ".join(bundled()) or "none"
        raise SkillError(f"no bundled skill {name!r} (bundled: {known})")
    return resources.files("labctl").joinpath("skills", name, SKILL_FILE).read_bytes()


def skills_dir(*, project: Optional[Path] = None, user: bool = False) -> Path:
    """Where Claude Code loads skills: the user's or a project's."""
    base = Path.home() if user else (project or Path.cwd())
    # The base is the caller's choice and may itself be reached through a
    # symlink (e.g. a symlinked home); below it, nothing is followed.
    return Path(os.path.realpath(base)) / ".claude" / "skills"


def target(name: str, *, project: Optional[Path] = None, user: bool = False) -> Path:
    return skills_dir(project=project, user=user) / name / SKILL_FILE


def _read_existing(dir_fd: int) -> Optional[bytes]:
    """The current ``SKILL.md`` in ``dir_fd``, or None. A symlink or other
    non-regular file there is refused, not read through."""
    try:
        st = os.stat(SKILL_FILE, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        raise SkillError(f"{SKILL_FILE} there is not a regular file; not touching it")
    fd = os.open(SKILL_FILE, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _open_skill_dir(path: Path, *, create: bool) -> int:
    try:
        return open_dir_nofollow(str(path), create=create)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SkillError(f"{path}: a symlink or file is in the way") from e
        raise


def install(
    name: str,
    *,
    project: Optional[Path] = None,
    user: bool = False,
    force: bool = False,
) -> Outcome:
    """Install the bundled skill ``name``. An existing ``SKILL.md`` that
    differs (an older version, or one edited by hand) is replaced only
    with ``force``."""
    content = text(name)
    path = target(name, project=project, user=user)
    dir_fd = _open_skill_dir(path.parent, create=True)
    try:
        existing = _read_existing(dir_fd)
        if existing == content:
            return Outcome("unchanged", path)
        if existing is not None and not force:
            raise SkillError(
                f"{path} exists and differs from the bundled skill (an older "
                "version, or edited); pass --force to replace it"
            )
        tmp = f".{SKILL_FILE}.{os.getpid()}.tmp"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=dir_fd,
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, SKILL_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)
    return Outcome("installed" if existing is None else "updated", path)


def uninstall(
    name: str,
    *,
    project: Optional[Path] = None,
    user: bool = False,
    force: bool = False,
) -> Outcome:
    """Remove an installed skill (and its directory, if then empty). A
    ``SKILL.md`` that differs from the bundled one is removed only with
    ``force``."""
    path = target(name, project=project, user=user)
    try:
        dir_fd = _open_skill_dir(path.parent, create=False)
    except FileNotFoundError:
        return Outcome("absent", path)
    try:
        existing = _read_existing(dir_fd)
        if existing is None:
            return Outcome("absent", path)
        if not force and name in bundled() and existing != text(name):
            raise SkillError(
                f"{path} differs from the bundled skill (edited?); pass "
                "--force to remove it"
            )
        os.unlink(SKILL_FILE, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        os.rmdir(path.parent)
    except OSError:
        pass  # other files in it: leave them
    return Outcome("removed", path)


def status(name: str, *, project: Optional[Path] = None, user: bool = False) -> str:
    """``current``, ``differs`` or ``not installed`` (read-only)."""
    path = target(name, project=project, user=user)
    try:
        dir_fd = _open_skill_dir(path.parent, create=False)
    except (FileNotFoundError, SkillError):
        return "not installed"
    try:
        existing = _read_existing(dir_fd)
    except SkillError:
        return "differs"
    finally:
        os.close(dir_fd)
    if existing is None:
        return "not installed"
    return "current" if existing == text(name) else "differs"


def write_deploy_config_template(project: Path) -> Optional[Path]:
    """Write an example ``.claude/deploy-config.json`` if there is none;
    returns its path, or None when one exists already."""
    base = Path(os.path.realpath(project))
    dir_fd = _open_skill_dir(base / ".claude", create=True)
    try:
        try:
            fd = os.open(
                DEPLOY_CONFIG.name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=dir_fd,
            )
        except FileExistsError:
            return None
        with os.fdopen(fd, "w") as f:
            json.dump(DEPLOY_CONFIG_EXAMPLE, f, indent=2)
            f.write("\n")
    finally:
        os.close(dir_fd)
    return base / DEPLOY_CONFIG

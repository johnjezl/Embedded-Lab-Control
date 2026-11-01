"""
Claude Code skills shipped with labctl, and installing them (#56).

The skills live in the package (``labctl/skills/<name>/SKILL.md``), so a
PyPI install has them. ``labctl skill install`` copies one to where Claude
Code loads it: ``<project>/.claude/skills/<name>/SKILL.md`` or
``~/.claude/skills/<name>/SKILL.md``.

The skills directory (``.claude/skills``) is resolved once, so a ``.claude``
kept in a dotfiles repository and symlinked in works. Below it nothing is
followed: ``<name>/`` is opened with ``O_NOFOLLOW`` (labctl.core.safe_open),
``SKILL.md`` is read without following or blocking, and the new file is
written to a temporary name and then linked (a first install, which so
can't overwrite a copy that appeared meanwhile) or renamed (``--force``)
into place, replacing a link rather than writing through it.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Optional

from labctl.core.safe_open import open_dir_nofollow, open_regular_nonblocking

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


def _base(project: Optional[Path], user: bool) -> Path:
    if user:
        return Path.home()
    base = project or Path.cwd()
    if not base.is_dir():
        raise SkillError(f"project directory {base} does not exist")
    return base


def skills_dir(*, project: Optional[Path] = None, user: bool = False) -> Path:
    """Where Claude Code loads skills: the user's or a project's, resolved
    (symlinks up to ``.claude/skills`` are the user's layout, e.g. a
    dotfiles repository)."""
    return Path(os.path.realpath(_base(project, user) / ".claude" / "skills"))


def target(name: str, *, project: Optional[Path] = None, user: bool = False) -> Path:
    return skills_dir(project=project, user=user) / name / SKILL_FILE


def _read_existing(dir_fd: int) -> Optional[bytes]:
    """The current ``SKILL.md`` in ``dir_fd``, or None. A symlink, FIFO or
    other non-regular file there is refused (never read through, never
    blocking on)."""
    try:
        fd = open_regular_nonblocking(SKILL_FILE, os.O_RDONLY, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EINVAL):
            raise SkillError(
                f"{SKILL_FILE} there is not a regular file; not touching it"
            ) from e
        raise
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _open_dir(path: Path, *, create: bool) -> int:
    """``path``'s directory (created with ``create``), the last component
    opened without following a symlink."""
    try:
        return open_dir_nofollow(str(path), create=create)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise SkillError(f"{path}: a symlink or file is in the way") from e
        if isinstance(e, FileNotFoundError):
            raise
        raise SkillError(f"{path}: {e.strerror}") from e


def _write_new(dir_fd: int, content: bytes, *, replace: bool) -> None:
    """Write ``content`` to ``SKILL.md`` in ``dir_fd`` via a temporary
    file: renamed over it with ``replace``, else linked, which fails if a
    ``SKILL.md`` appeared since it was checked (no silent overwrite)."""
    tmp = f".{SKILL_FILE}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dir_fd
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        if replace:
            os.replace(tmp, SKILL_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            return
        try:
            os.link(tmp, SKILL_FILE, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileExistsError as e:
            raise SkillError(
                f"{SKILL_FILE} appeared while installing; run install again"
            ) from e
    finally:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass  # renamed into place


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
    dir_fd = _open_dir(path.parent, create=True)
    try:
        try:
            existing = _read_existing(dir_fd)
            if existing == content:
                return Outcome("unchanged", path)
            if existing is not None and not force:
                raise SkillError(
                    f"{path} exists and differs from the bundled skill (an "
                    "older version, or edited); pass --force to replace it"
                )
            _write_new(dir_fd, content, replace=existing is not None)
        except OSError as e:
            raise SkillError(f"{path}: {e.strerror or e}") from e
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
    """Remove an installed bundled skill (and its directory, if then
    empty). Only labctl's own skills: another skill of the same layout is
    never touched. A copy that differs from the bundled one is removed
    only with ``force``."""
    content = text(name)  # SkillError for a skill labctl doesn't ship
    path = target(name, project=project, user=user)
    try:
        skills_fd = _open_dir(path.parent.parent, create=False)
    except FileNotFoundError:
        return Outcome("absent", path)
    try:
        try:
            dir_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skills_fd
            )
        except FileNotFoundError:
            return Outcome("absent", path)
        except OSError as e:
            raise SkillError(f"{path.parent}: a symlink or file is in the way") from e
        try:
            existing = _read_existing(dir_fd)
            if existing is None:
                return Outcome("absent", path)
            if not force and existing != content:
                raise SkillError(
                    f"{path} differs from the bundled skill (edited?); pass "
                    "--force to remove it"
                )
            os.unlink(SKILL_FILE, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        try:
            os.rmdir(name, dir_fd=skills_fd)
        except OSError:
            pass  # other files in it: leave them
    finally:
        os.close(skills_fd)
    return Outcome("removed", path)


def status(name: str, *, project: Optional[Path] = None, user: bool = False) -> str:
    """``current``, ``differs``, ``not installed`` or ``unreadable`` (with
    why) for an install of ``name``; read-only."""
    try:
        path = target(name, project=project, user=user)
        dir_fd = _open_dir(path.parent, create=False)
    except FileNotFoundError:
        return "not installed"
    except SkillError as e:
        return f"unreadable ({e})"
    try:
        existing = _read_existing(dir_fd)
    except (SkillError, OSError) as e:
        return f"unreadable ({getattr(e, 'strerror', None) or e})"
    finally:
        os.close(dir_fd)
    if existing is None:
        return "not installed"
    return "current" if existing == text(name) else "differs"


def write_deploy_config_template(project: Path) -> Optional[Path]:
    """Write an example ``.claude/deploy-config.json`` if there is none;
    returns its path, or None when one exists already."""
    claude = Path(os.path.realpath(_base(project, False) / ".claude"))
    dir_fd = _open_dir(claude, create=True)
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
        except OSError as e:
            raise SkillError(f"{claude / DEPLOY_CONFIG.name}: {e.strerror}") from e
        with os.fdopen(fd, "w") as f:
            json.dump(DEPLOY_CONFIG_EXAMPLE, f, indent=2)
            f.write("\n")
    finally:
        os.close(dir_fd)
    return claude / DEPLOY_CONFIG.name

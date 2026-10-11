"""
Root-run file operations for the install and update scripts.

scripts/install-services.sh and scripts/update.sh run as root but create,
copy and re-permission files under /var/lib/labctl, a tree the ``labctl``
user (the identity of the web and MCP services) can write to. Path-based
``chown``/``chmod``/``cp`` follow a symlink planted there, so a compromised
service could have root hand it ``/etc/labctl/config.yaml``, or copy
``/etc/shadow`` into a config file its group can read.

Everything here works on descriptors opened without following symlinks on
any path component (:mod:`labctl.core.safe_open`), requires a regular file
with a single link, and creates files with ``O_EXCL``. Callers pass paths
whose trusted prefix (the labctl home, which an admin may have symlinked
elsewhere) is already resolved. Command line::

    python -m labctl.core.admin_files state PATH
        prints "missing" or "regular"; exit 2 (with the reason) otherwise
    python -m labctl.core.admin_files secure PATH USER GROUP MODE
    python -m labctl.core.admin_files secure-dir PATH USER GROUP MODE
        (creates the directory if missing)
    python -m labctl.core.admin_files copy SRC DST MODE [OLD_LINE NEW_LINE]...
        DST must not exist; lines equal to OLD_LINE are replaced
"""

from __future__ import annotations

import errno
import grp
import os
import pwd
import sys

from labctl.core.safe_open import open_dir_nofollow, open_file_nofollow


class UnsafeFileError(OSError):
    """A path that root must not touch by name (symlink, hard link...)."""


def _check_single_link(fd: int, path: str) -> None:
    if os.fstat(fd).st_nlink != 1:
        raise UnsafeFileError(errno.EPERM, f"{path} has more than one hard link")


def _nofollow(path: str, opener, *args, **kwargs) -> int:
    """Call a safe_open opener, turning "not followed" errors into
    UnsafeFileError so every refusal reads the same."""
    try:
        return opener(*args, **kwargs)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeFileError(e.errno, f"{path}: symlink on the path") from e
        if e.errno == errno.EINVAL:
            raise UnsafeFileError(e.errno, f"{path} is not a regular file") from e
        if e.errno == errno.EEXIST:
            raise UnsafeFileError(e.errno, f"{path} already exists") from e
        raise


def open_regular(path: str, flags: int = os.O_RDONLY) -> int:
    """Open a regular, singly linked file with no symlink on its path."""
    fd = _nofollow(path, open_file_nofollow, path, flags)
    try:
        _check_single_link(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def state(path: str) -> str:
    """'missing' or 'regular'; raises UnsafeFileError for anything else.

    The parent directory is reached without following symlinks too, so a
    symlinked parent is refused rather than reported as "missing".
    """
    parent = _nofollow(path, open_dir_nofollow, os.path.dirname(path))
    try:
        os.stat(os.path.basename(path), dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    finally:
        os.close(parent)
    os.close(open_regular(path))
    return "regular"


def _ids(user: str, group: str) -> tuple[int, int]:
    return pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid


def secure(path: str, user: str, group: str, mode: int) -> None:
    """chown/chmod a regular file through a descriptor (no symlinks)."""
    uid, gid = _ids(user, group)
    fd = open_regular(path)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def secure_dir(path: str, user: str, group: str, mode: int) -> None:
    """Create (if needed) and chown/chmod a directory, no symlinks."""
    uid, gid = _ids(user, group)
    fd = _nofollow(path, open_dir_nofollow, path, create=True, mode=0o700)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def copy(src: str, dst: str, mode: int, replace: dict[str, str] | None = None) -> None:
    """Copy a regular file to a new file (``O_EXCL``), neither via symlinks.

    Byte for byte (line endings and encoding kept, so a later ``cmp`` of
    the two matches), except that lines equal to a key of ``replace``
    become its value.
    """
    fd = open_regular(src)
    try:
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read()
    finally:
        os.close(fd)
    if replace:
        swap = {k.encode(): v.encode() for k, v in replace.items()}
        data = b"\n".join(swap.get(line, line) for line in data.split(b"\n"))
    parent = _nofollow(dst, open_dir_nofollow, os.path.dirname(dst))
    try:
        out = _nofollow(
            dst,
            os.open,
            os.path.basename(dst),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
    finally:
        os.close(parent)
    try:
        os.fchmod(out, mode)  # not narrowed by the umask
        with os.fdopen(out, "wb", closefd=False) as f:
            f.write(data)
    finally:
        os.close(out)


def main(argv: list[str]) -> int:
    cmd, *args = argv
    try:
        if cmd == "state" and len(args) == 1:
            print(state(args[0]))
        elif cmd == "secure" and len(args) == 4:
            secure(args[0], args[1], args[2], int(args[3], 8))
        elif cmd == "secure-dir" and len(args) == 4:
            secure_dir(args[0], args[1], args[2], int(args[3], 8))
        elif cmd == "copy" and len(args) >= 3 and len(args) % 2 == 1:
            pairs = args[3:]
            copy(args[0], args[1], int(args[2], 8), dict(zip(pairs[::2], pairs[1::2])))
        else:
            print(__doc__, file=sys.stderr)
            return 64
    except UnsafeFileError as e:
        print(
            f"refusing: {e.strerror}", file=sys.stdout if cmd == "state" else sys.stderr
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

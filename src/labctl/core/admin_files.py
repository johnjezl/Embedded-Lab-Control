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
    python -m labctl.core.admin_files create PATH USER GROUP MODE
        creates an empty file if missing, then secures it like `secure`
    python -m labctl.core.admin_files shared-db SYSTEM_CONFIG DB_DIR USER GROUP
        when SYSTEM_CONFIG's database_path is DB_DIR/labctl.db, makes DB_DIR
        setgid 2775, the database (and any -wal/-shm) 664 and DB_DIR's parent
        searchable by GROUP (g+x if missing); prints what
        it did; exit 2 after a warning, never stopping half-way silently
    python -m labctl.core.admin_files copy SRC DST MODE [OLD_LINE NEW_LINE]...
        DST must not exist; lines equal to OLD_LINE are replaced
"""

from __future__ import annotations

import errno
import grp
import os
import pwd
import stat
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
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        # safe_open would also raise EINVAL: say what is actually wrong.
        raise UnsafeFileError(
            errno.EINVAL, f"{path}: path must be absolute and normalized"
        )
    try:
        return opener(*args, **kwargs)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeFileError(
                e.errno, f"{path}: a symlink or non-directory on the path"
            ) from e
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

    One walk with no symlink followed on any component: a symlinked parent
    is refused (not "missing"); a missing file or parent is "missing".
    """
    try:
        fd = open_regular(path)
    except FileNotFoundError:
        return "missing"
    os.close(fd)
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
    """Create (if needed) and chown/chmod a directory, no symlinks.

    Only the last component is created: a missing parent is an error, not
    a reason to build a root-owned chain (e.g. the labctl home itself).
    """
    uid, gid = _ids(user, group)
    parent = _nofollow(path, open_dir_nofollow, os.path.dirname(path))
    name = os.path.basename(path)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass  # existing (or concurrently created): the open re-checks it
        fd = _nofollow(
            path,
            os.open,
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    finally:
        os.close(parent)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def create(path: str, user: str, group: str, mode: int) -> None:
    """Create an empty file (``O_EXCL``, no symlinks) unless a regular one
    is there, then chown/chmod it like :func:`secure`."""
    parent = _nofollow(path, open_dir_nofollow, os.path.dirname(path))
    name = os.path.basename(path)
    try:
        try:
            fd = _nofollow(
                path,
                os.open,
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            # secure() below refuses anything but a regular file.
        else:
            os.close(fd)
    finally:
        os.close(parent)
    secure(path, user, group, mode)


DB_NAME = "labctl.db"


def _configured_database_path(system_config: str) -> str:
    """``database_path`` from the (root-owned) system config, or ""."""
    import yaml

    with open(system_config) as f:
        data = yaml.safe_load(f)
    value = data.get("database_path", "") if isinstance(data, dict) else ""
    return value if isinstance(value, str) else ""


def _is_db_in(configured: str, db_dir: str) -> bool:
    """Whether ``configured`` (database_path) names ``db_dir/labctl.db``.

    Its directory part is compared resolved, so a symlinked
    /var/lib/labctl (or a path written through one) still matches the
    resolved ``db_dir``; the file name is compared literally and the
    database file itself is never resolved or followed.
    """
    if not configured or not os.path.isabs(configured):
        return False
    directory, name = os.path.split(configured)
    if name != DB_NAME:
        return False
    resolved = os.path.realpath(directory)
    return resolved in (os.path.normpath(db_dir), os.path.realpath(db_dir))


def _ensure_group_search(home: str, gid: int) -> str | None:
    """Make sure members of group ``gid`` can traverse ``home`` (the
    labctl home, already resolved, in root-owned /var/lib).

    If ``home`` belongs to that group and lacks group execute, add it (only
    ``g+x``, through a descriptor opened without following symlinks).
    Returns a reason when group members still can't search it (e.g. it
    belongs to another group), else None.
    """
    fd = open_dir_nofollow(home)
    try:
        st = os.fstat(fd)
        mode = stat.S_IMODE(st.st_mode)
        if st.st_gid == gid and not mode & stat.S_IXGRP:
            os.fchmod(fd, mode | stat.S_IXGRP)
            print(f"[ok] {home}: added group search (g+x) for the database")
            mode |= stat.S_IXGRP
    finally:
        os.close(fd)
    if (st.st_gid == gid and mode & stat.S_IXGRP) or mode & stat.S_IXOTH:
        return None
    try:
        owner_group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        owner_group = str(st.st_gid)
    return (
        f"{home} (mode {mode:04o}, group {owner_group}) can't be traversed by "
        "the group's members, so they can't reach the database"
    )


def shared_db(system_config: str, db_dir: str, user: str, group: str) -> int:
    """Keep the services' database writable by ``group`` (install-services.sh
    and update.sh both call this).

    Only when the system config's ``database_path`` is ``db_dir/labctl.db``:
    ``db_dir`` setgid ``2775`` (the -wal/-shm files SQLite creates are then
    in the group, with the database's mode; not sticky, so whichever
    process closes the database last can remove them), the database
    (created empty if missing) and any existing -wal/-shm ``664``, and the
    labctl home (``db_dir``'s parent) searchable by the group (``g+x``
    added if missing, nothing else). A symlinked ``db_dir`` (an admin's
    relocation) is left alone. Prints one line per outcome; returns 0, or
    2 after printing a warning, so the calling script carries on.
    """
    import yaml

    db_path = os.path.join(db_dir, DB_NAME)
    try:
        configured = _configured_database_path(system_config)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        print(
            f"[!!] Database permissions not checked: can't read "
            f"database_path from {system_config}: {e}"
        )
        return 2
    if not _is_db_in(configured, db_dir):
        print(f"[--] The services' database ({configured or 'database_path unset'})")
        print(f"     isn't {db_path}, so {group} group members can't write it from")
        print('     the CLI. To move it there, see "Moving the database" in README.md.')
        return 0
    if os.path.islink(db_dir):
        print(f"[ok] {db_dir} is a symlink; leaving it (and its permissions) as is")
        return 0
    problems = []
    try:
        secure_dir(db_dir, user, group, 0o2775)
        create(db_path, user, group, 0o664)
        unreachable = _ensure_group_search(
            os.path.dirname(db_dir), grp.getgrnam(group).gr_gid
        )
    except (OSError, KeyError) as e:
        print(f"[!!] Database {db_path} not made group-writable: {_reason(e)}")
        return 2
    if unreachable:
        problems.append(unreachable)
    for extra in (db_path + "-wal", db_path + "-shm"):
        try:
            if state(extra) == "regular":
                secure(extra, user, group, 0o664)
        except OSError as e:
            problems.append(f"Left as is: {extra}: {_reason(e)}")
    if problems:
        for problem in problems:
            print(f"[!!] {problem}")
        return 2
    print(f"[ok] Database {db_path} ({user}:{group}, 664; group-writable directory)")
    return 0


def _reason(e: BaseException) -> str:
    if isinstance(e, KeyError):
        return f"unknown user or group {e}"
    if isinstance(e, UnsafeFileError):
        return f"refusing: {e.strerror}"
    return getattr(e, "strerror", None) or str(e)


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
    name = os.path.basename(dst)
    try:
        out = _nofollow(
            dst,
            os.open,
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=parent,
        )
        try:
            os.fchmod(out, mode)  # not narrowed by the umask
            with os.fdopen(out, "wb", closefd=False) as f:
                f.write(data)
            os.fsync(out)
        except BaseException:
            # Never leave a truncated config that a rerun would take as
            # complete ("already exists, skipping"); keep the real error.
            try:
                os.unlink(name, dir_fd=parent)
            except OSError:
                pass
            raise
        finally:
            os.close(out)
    finally:
        os.close(parent)


def main(argv: list[str]) -> int:
    """Exit 0 on success, 2 with a one-line reason for any refusal or
    error (on stdout for `state`, which the scripts capture; stderr
    otherwise), 64 for bad usage."""
    if not argv:
        print(__doc__, file=sys.stderr)
        return 64
    cmd, *args = argv
    try:
        if cmd == "state" and len(args) == 1:
            print(state(args[0]))
        elif cmd == "secure" and len(args) == 4:
            secure(args[0], args[1], args[2], int(args[3], 8))
        elif cmd == "secure-dir" and len(args) == 4:
            secure_dir(args[0], args[1], args[2], int(args[3], 8))
        elif cmd == "create" and len(args) == 4:
            create(args[0], args[1], args[2], int(args[3], 8))
        elif cmd == "shared-db" and len(args) == 4:
            return shared_db(*args)
        elif cmd == "copy" and len(args) >= 3 and len(args) % 2 == 1:
            pairs = args[3:]
            copy(args[0], args[1], int(args[2], 8), dict(zip(pairs[::2], pairs[1::2])))
        else:
            print(__doc__, file=sys.stderr)
            return 64
    except UnsafeFileError as e:
        reason = f"refusing: {e.strerror}"
    except OSError as e:
        # The paths asked about, not e.filename (often one path component).
        paths = " -> ".join(args[:2] if cmd == "copy" else args[:1])
        reason = f"error: {paths}: {e.strerror or e}"
    except KeyError as e:  # unknown user or group
        reason = f"error: unknown user or group {e}"
    except ValueError as e:  # bad mode
        reason = f"error: {e}"
    else:
        return 0
    print(reason, file=sys.stdout if cmd == "state" else sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

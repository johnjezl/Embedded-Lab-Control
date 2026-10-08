"""
Open host paths without following symlinks, for check-then-use safety.

The MCP host-path allowlist resolves a path and checks it against allowed
directories. If the file were then re-opened *by path* later (after a
power-off, a mux switch, a wait for the reader...), anyone able to write to
a directory on that path could swap in a symlink in between and redirect
the read or write; raw images are even read by ``sudo dd`` (root). These
helpers open the already-resolved path one component at a time with
``O_NOFOLLOW``, so a symlink anywhere on the path makes the open fail
instead of being followed. Callers then use the returned file descriptor,
never the path, for the actual I/O.
"""

import errno
import fcntl
import os
import stat

# Intermediate directories need only search permission with O_PATH (Linux);
# fall back to O_RDONLY elsewhere. O_DIRECTORY|O_NOFOLLOW makes a symlinked
# component fail with ENOTDIR/ELOOP rather than be followed.
_WALK_FLAGS = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW


def _components(path: str) -> list[str]:
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute: {path!r}")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise OSError(errno.EINVAL, "path must be normalized (no '.' or '..')")
    return parts


def _walk(parts: list[str], *, create: bool, mode: int) -> int:
    """Open the directory reached by ``parts`` from "/", no symlinks."""
    fd = os.open("/", _WALK_FLAGS)
    try:
        for name in parts:
            try:
                nxt = os.open(name, _WALK_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(name, mode, dir_fd=fd)
                except FileExistsError:
                    pass  # created concurrently; the open below re-checks it
                nxt = os.open(name, _WALK_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_regular_nonblocking(
    name: str, flags: int, mode: int = 0o644, dir_fd: int | None = None
) -> int:
    """Open ``name`` (O_NOFOLLOW), insisting it is a regular file.

    Opened with O_NONBLOCK so a FIFO planted in a shared directory can't
    block the open (which would hang the caller while it holds locks);
    anything that isn't a regular file is closed and refused with OSError;
    O_NONBLOCK is then cleared for normal I/O.
    """
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, f"not a regular file: {name}")
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) & ~os.O_NONBLOCK)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_file_nofollow(path: str, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
    """Open the regular file at absolute, normalized ``path``, no symlinks.

    Raises OSError (ELOOP/ENOTDIR) if any component is a symlink, and
    (EINVAL) if the target isn't a regular file (e.g. a FIFO); never blocks
    on opening a FIFO.
    """
    parts = _components(path)
    if not parts:
        raise IsADirectoryError(errno.EISDIR, "path is the root directory")
    parent = _walk(parts[:-1], create=False, mode=0o755)
    try:
        return open_regular_nonblocking(parts[-1], flags, mode, dir_fd=parent)
    finally:
        os.close(parent)


def open_dir_nofollow(path: str, *, create: bool = False, mode: int = 0o775) -> int:
    """Open (optionally creating) directory ``path`` with no symlink anywhere.

    Returns an ``O_RDONLY | O_DIRECTORY`` descriptor usable as ``dir_fd``
    for creating files inside it.
    """
    parts = _components(path)
    walked = _walk(parts, create=create, mode=mode)
    try:
        # Re-open "." relative to the walked handle as a regular directory
        # fd (an O_PATH fd can't be used for everything on every kernel).
        return os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=walked)
    finally:
        os.close(walked)

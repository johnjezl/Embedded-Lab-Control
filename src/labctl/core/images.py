"""
Images uploaded over MCP HTTP (D014).

An MCP tool's arguments are text the model writes out, so a disk image
can't travel in one. Agents upload with their own shell instead
(``PUT /images/<name>`` on the MCP HTTP server, the same API key), into
``mcp.upload_dir``; tools then take the bare name. This module is the
store behind that route and the ``list_images`` / ``delete_image`` tools.

- Names are bare file names (no paths, no leading dot), so nothing can be
  written outside the directory, and upload temp files (``.upload-*``)
  never collide with an image.
- An upload is written to a temp file and renamed into place only when
  complete (and its SHA-256 matches, if the uploader gave one): a broken
  upload never appears as an image, and a tool reading the old file by
  descriptor keeps reading the old file.
- The ``images`` table records who uploaded each image. Replacing or
  deleting an image is allowed to its uploader and to operators; files put
  in the directory another way have no uploader, so only operators may
  replace or delete them.
- The directory is opened without following symlinks, and files in it are
  created and renamed relative to that descriptor.
"""

import hashlib
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from typing import Optional

from labctl.core.safe_open import open_dir_nofollow

# A name a shell, URL and SD-card file system all take without quoting.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
TEMP_PREFIX = ".upload-"
# Space left free on the upload file system after an upload.
FREE_SPACE_RESERVE_BYTES = 1 << 30
# Temp files of uploads that died (server killed mid-upload) are removed
# by the next upload once they are this old.
STALE_TEMP_SECONDS = 24 * 3600
_CHUNK = 1 << 20

# Serializes the replace/delete decision with the rename or unlink it
# allows, and the record that follows, within this process.
_commit_lock = threading.Lock()


class ImageError(Exception):
    """A refused image operation: ``code`` and HTTP ``status`` for the
    route, ``message`` for people."""

    def __init__(self, code: str, status: int, message: str):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


def name_error(name) -> Optional[str]:
    """Why ``name`` can't be an image name, or None."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        return (
            f"invalid image name {name!r}: use 1-128 letters, digits, '.', "
            "'_', '+' or '-', starting with a letter or digit (no paths)"
        )
    return None


@dataclass
class Caller:
    """Who is acting: ``user`` is None without auth (one shared identity,
    an operator)."""

    user: Optional[str]
    operator: bool


class ImageStore:
    def __init__(self, directory: str, db):
        self.directory = directory
        self.db = db

    # -- helpers ---------------------------------------------------------

    def _open_dir(self) -> int:
        try:
            return open_dir_nofollow(self.directory, create=False)
        except OSError as e:
            raise ImageError(
                "upload_dir_unavailable",
                500,
                f"upload directory {self.directory} can't be opened: "
                f"{e.strerror or e}",
            ) from e

    def _record(self, name: str):
        return self.db.execute_one(
            "SELECT name, uploaded_by, size, sha256, uploaded_at FROM images"
            " WHERE name = ?",
            (name,),
        )

    def _existing(self, dir_fd: int, name: str) -> Optional[os.stat_result]:
        try:
            st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return st

    def _check_may_replace(self, name: str, caller: Caller, verb: str) -> None:
        """Refuse ``verb`` ("replace"/"delete") on an existing image the
        caller didn't upload, unless they are an operator."""
        if caller.operator:
            return
        record = self._record(name)
        owner = record["uploaded_by"] if record else None
        if owner is None or owner != caller.user:
            by = f"uploaded by '{owner}'" if owner else "not uploaded over MCP"
            raise ImageError(
                "not_owner",
                403,
                f"image '{name}' was {by}; only its uploader or an operator "
                f"may {verb} it",
            )

    # -- listing ---------------------------------------------------------

    def list(self) -> list[dict]:
        """The images in the directory (regular files, no dot files), with
        who uploaded them where known."""
        dir_fd = self._open_dir()
        try:
            names = sorted(os.listdir(dir_fd))
            records = {
                r["name"]: r
                for r in self.db.execute(
                    "SELECT name, uploaded_by, size, sha256, uploaded_at FROM images"
                )
            }
            images = []
            for name in names:
                if name.startswith("."):
                    continue
                st = self._existing(dir_fd, name)
                if st is None or not stat.S_ISREG(st.st_mode):
                    continue
                record = records.get(name)
                # A record describes this file only while the file is the
                # one uploaded (same size); a file replaced another way
                # keeps no uploader or checksum.
                if record is not None and record["size"] != st.st_size:
                    record = None
                images.append(
                    {
                        "name": name,
                        "path": os.path.join(self.directory, name),
                        "size": st.st_size,
                        "modified": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)
                        ),
                        "uploaded_by": record["uploaded_by"] if record else None,
                        "sha256": record["sha256"] if record else None,
                    }
                )
            return images
        finally:
            os.close(dir_fd)

    # -- upload ----------------------------------------------------------

    def begin_upload(
        self,
        name: str,
        size: int,
        caller: Caller,
        *,
        overwrite: bool = False,
        max_bytes: int,
        expected_sha256: Optional[str] = None,
    ) -> "Upload":
        """Check an upload of ``size`` bytes may go ahead, and open its temp
        file. Raises ImageError."""
        err = name_error(name)
        if err:
            raise ImageError("invalid_name", 400, err)
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ImageError(
                    "invalid_sha256", 400, "sha256 must be 64 hexadecimal digits"
                )
        if size < 0:
            raise ImageError("invalid_length", 400, "Content-Length is negative")
        if size > max_bytes:
            raise ImageError(
                "too_large",
                413,
                f"{size} bytes is over the upload limit of {max_bytes} bytes "
                "(mcp.max_upload_mb)",
            )
        dir_fd = self._open_dir()
        try:
            existing = self._existing(dir_fd, name)
            if existing is not None:
                if not stat.S_ISREG(existing.st_mode):
                    raise ImageError(
                        "not_a_file", 409, f"'{name}' exists and is not a file"
                    )
                if not overwrite:
                    raise ImageError(
                        "exists",
                        409,
                        f"image '{name}' already exists; repeat with "
                        "overwrite=1 to replace it",
                    )
                self._check_may_replace(name, caller, "replace")
            _remove_stale_temps(dir_fd)
            fs = os.statvfs(dir_fd)
            free = fs.f_bavail * fs.f_frsize
            if size + FREE_SPACE_RESERVE_BYTES > free:
                raise ImageError(
                    "insufficient_space",
                    507,
                    f"{size} bytes would leave less than "
                    f"{FREE_SPACE_RESERVE_BYTES >> 20} MiB free on the upload "
                    f"file system ({free} bytes free)",
                )
            temp = f"{TEMP_PREFIX}{secrets.token_hex(8)}"
            fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o664,
                dir_fd=dir_fd,
            )
        except BaseException:
            os.close(dir_fd)
            raise
        return Upload(
            self,
            dir_fd,
            fd,
            temp,
            name,
            size,
            caller,
            overwrite,
            expected_sha256,
        )

    # -- delete ----------------------------------------------------------

    def delete(self, name: str, caller: Caller) -> None:
        err = name_error(name)
        if err:
            raise ImageError("invalid_name", 400, err)
        dir_fd = self._open_dir()
        try:
            with _commit_lock:
                existing = self._existing(dir_fd, name)
                if existing is None:
                    raise ImageError("not_found", 404, f"no image named '{name}'")
                if not stat.S_ISREG(existing.st_mode):
                    raise ImageError(
                        "not_a_file", 409, f"'{name}' exists and is not a file"
                    )
                self._check_may_replace(name, caller, "delete")
                os.unlink(name, dir_fd=dir_fd)
                self.db.execute_modify("DELETE FROM images WHERE name = ?", (name,))
        finally:
            os.close(dir_fd)


class Upload:
    """One upload in progress: write() the body, then commit() or abort()."""

    def __init__(
        self,
        store: ImageStore,
        dir_fd: int,
        fd: int,
        temp: str,
        name: str,
        size: int,
        caller: Caller,
        overwrite: bool,
        expected_sha256: Optional[str],
    ):
        self.store = store
        self.dir_fd = dir_fd
        self.fd = fd
        self.temp = temp
        self.name = name
        self.size = size
        self.caller = caller
        self.overwrite = overwrite
        self.expected_sha256 = expected_sha256
        self.received = 0
        self._hash = hashlib.sha256()
        self._done = False

    def write(self, chunk: bytes) -> None:
        self.received += len(chunk)
        if self.received > self.size:
            raise ImageError(
                "length_mismatch", 400, "body is longer than Content-Length"
            )
        self._hash.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(self.fd, view[:_CHUNK])
            view = view[written:]

    def commit(self) -> dict:
        """Move the finished upload into place and record it."""
        try:
            if self.received != self.size:
                raise ImageError(
                    "length_mismatch",
                    400,
                    f"received {self.received} of {self.size} bytes",
                )
            digest = self._hash.hexdigest()
            if self.expected_sha256 and digest != self.expected_sha256:
                raise ImageError(
                    "sha256_mismatch",
                    422,
                    f"uploaded data has SHA-256 {digest}, not "
                    f"{self.expected_sha256}; nothing was stored",
                )
            os.fsync(self.fd)
            os.close(self.fd)
            self.fd = -1
            store, name = self.store, self.name
            with _commit_lock:
                # Checked again: the image may have appeared, or changed
                # hands, while the body was arriving.
                existing = store._existing(self.dir_fd, name)
                if existing is not None:
                    if not stat.S_ISREG(existing.st_mode):
                        raise ImageError(
                            "not_a_file", 409, f"'{name}' exists and is not a file"
                        )
                    if not self.overwrite:
                        raise ImageError(
                            "exists",
                            409,
                            f"image '{name}' was created while uploading; "
                            "repeat with overwrite=1 to replace it",
                        )
                    store._check_may_replace(name, self.caller, "replace")
                os.rename(
                    self.temp,
                    name,
                    src_dir_fd=self.dir_fd,
                    dst_dir_fd=self.dir_fd,
                )
                self._done = True
                store.db.execute_modify(
                    "INSERT INTO images (name, uploaded_by, size, sha256,"
                    " uploaded_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)"
                    " ON CONFLICT(name) DO UPDATE SET uploaded_by = excluded."
                    "uploaded_by, size = excluded.size, sha256 = excluded.sha256,"
                    " uploaded_at = excluded.uploaded_at",
                    (name, self.caller.user, self.size, digest),
                )
            try:
                os.fsync(self.dir_fd)
            except OSError:
                pass  # best effort: the rename is already visible
            return {
                "status": "uploaded",
                "name": name,
                "path": os.path.join(store.directory, name),
                "size": self.size,
                "sha256": digest,
                "replaced": existing is not None,
                "uploaded_by": self.caller.user,
            }
        finally:
            self.abort()

    def abort(self) -> None:
        """Drop the temp file (no-op after a successful commit)."""
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
        if not self._done:
            self._done = True
            try:
                os.unlink(self.temp, dir_fd=self.dir_fd)
            except OSError:
                pass
        if self.dir_fd >= 0:
            os.close(self.dir_fd)
            self.dir_fd = -1


def _remove_stale_temps(dir_fd: int) -> None:
    cutoff = time.time() - STALE_TEMP_SECONDS
    for entry in os.listdir(dir_fd):
        if not entry.startswith(TEMP_PREFIX):
            continue
        try:
            st = os.stat(entry, dir_fd=dir_fd, follow_symlinks=False)
            if st.st_mtime < cutoff:
                os.unlink(entry, dir_fd=dir_fd)
        except OSError:
            pass  # gone already, or not ours to remove

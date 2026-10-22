"""Image uploads over MCP HTTP (D014): the store, the PUT /images route,
list_images / delete_image, bare image names in tools, and TLS."""

import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from labctl.core.config import AuthConfig, Config, UserConfig
from labctl.core.database import get_database
from labctl.core.images import (
    TEMP_PREFIX,
    Caller,
    ImageError,
    ImageStore,
    name_error,
)

ALICE_KEY = "alice-key-0123456789abcdef"  # operator
BOT_KEY = "bot-key-0123456789abcdefgh"  # agent
BOT2_KEY = "bot2-key-0123456789abcdefg"  # agent

ALICE = Caller(user="alice", operator=True)
BOT = Caller(user="bot", operator=False)
BOT2 = Caller(user="bot2", operator=False)


@pytest.fixture
def db(tmp_path):
    return get_database(tmp_path / "lab.db")


@pytest.fixture
def images_dir(tmp_path):
    path = tmp_path / "images"
    path.mkdir()
    return path


@pytest.fixture
def store(images_dir, db):
    return ImageStore(str(images_dir), db)


def _put(store, name, data, caller, **kwargs):
    kwargs.setdefault("max_bytes", 1 << 30)
    upload = store.begin_upload(name, len(data), caller, **kwargs)
    try:
        upload.write(data)
        return upload.commit()
    finally:
        upload.abort()


def _temps(images_dir):
    return [p for p in os.listdir(images_dir) if p.startswith(TEMP_PREFIX)]


class TestNames:
    @pytest.mark.parametrize(
        "name", ["sdcard.img", "zImage", "a", "build-1.2_rc+x.img.xz", "A" * 128]
    )
    def test_valid(self, name):
        assert name_error(name) is None

    @pytest.mark.parametrize(
        "name",
        [
            "",
            ".hidden",
            ".upload-abc",
            "../x",
            "a/b",
            "/abs",
            "a b",
            "-rf",
            "x\0y",
            "é.img",
            "A" * 129,
            None,
            7,
        ],
    )
    def test_invalid(self, name):
        assert name_error(name) is not None


class TestStoreUpload:
    def test_upload_stores_and_records(self, store, images_dir, db):
        result = _put(store, "a.img", b"hello", BOT)
        assert (images_dir / "a.img").read_bytes() == b"hello"
        assert result["sha256"] == hashlib.sha256(b"hello").hexdigest()
        assert result["replaced"] is False
        assert result["uploaded_by"] == "bot"
        row = db.execute_one("SELECT * FROM images WHERE name = 'a.img'")
        assert row["uploaded_by"] == "bot" and row["size"] == 5
        assert _temps(images_dir) == []

    def test_existing_refused_without_overwrite(self, store, images_dir):
        _put(store, "a.img", b"one", BOT)
        with pytest.raises(ImageError) as e:
            _put(store, "a.img", b"two", BOT)
        assert e.value.code == "exists" and e.value.status == 409
        assert (images_dir / "a.img").read_bytes() == b"one"

    def test_uploader_may_replace(self, store, images_dir):
        _put(store, "a.img", b"one", BOT)
        result = _put(store, "a.img", b"two", BOT, overwrite=True)
        assert result["replaced"] is True
        assert (images_dir / "a.img").read_bytes() == b"two"

    def test_other_agent_may_not_replace(self, store, images_dir):
        _put(store, "a.img", b"one", BOT)
        with pytest.raises(ImageError) as e:
            _put(store, "a.img", b"two", BOT2, overwrite=True)
        assert e.value.code == "not_owner" and e.value.status == 403
        assert "uploaded by 'bot'" in e.value.message
        assert (images_dir / "a.img").read_bytes() == b"one"
        assert _temps(images_dir) == []

    def test_other_agent_refused_before_the_body(self, store, images_dir):
        _put(store, "a.img", b"one", BOT)
        with pytest.raises(ImageError) as e:
            store.begin_upload("a.img", 3, BOT2, overwrite=True, max_bytes=99)
        assert e.value.code == "not_owner"
        assert _temps(images_dir) == []

    def test_operator_may_replace_and_becomes_uploader(self, store, db):
        _put(store, "a.img", b"one", BOT)
        _put(store, "a.img", b"two", ALICE, overwrite=True)
        row = db.execute_one("SELECT uploaded_by FROM images WHERE name = 'a.img'")
        assert row["uploaded_by"] == "alice"

    def test_file_put_there_another_way_is_operator_only(self, store, images_dir):
        (images_dir / "a.img").write_bytes(b"scp")
        with pytest.raises(ImageError) as e:
            _put(store, "a.img", b"two", BOT, overwrite=True)
        assert e.value.code == "not_owner"
        assert "not uploaded over MCP" in e.value.message
        _put(store, "a.img", b"two", ALICE, overwrite=True)

    def test_owner_checked_again_at_commit(self, store, images_dir):
        """The image changed hands while the body was arriving."""
        _put(store, "a.img", b"one", BOT)
        upload = store.begin_upload("a.img", 3, BOT, overwrite=True, max_bytes=99)
        _put(store, "a.img", b"op", ALICE, overwrite=True)
        upload.write(b"two")
        with pytest.raises(ImageError) as e:
            upload.commit()
        assert e.value.code == "not_owner"
        assert (images_dir / "a.img").read_bytes() == b"op"
        assert _temps(images_dir) == []

    def test_created_meanwhile_refused_without_overwrite(self, store, images_dir):
        upload = store.begin_upload("a.img", 3, BOT, max_bytes=99)
        _put(store, "a.img", b"one", BOT2)
        upload.write(b"two")
        with pytest.raises(ImageError) as e:
            upload.commit()
        assert e.value.code == "exists"
        assert (images_dir / "a.img").read_bytes() == b"one"

    def test_sha256_mismatch_stores_nothing(self, store, images_dir, db):
        with pytest.raises(ImageError) as e:
            _put(store, "a.img", b"data", BOT, expected_sha256="0" * 64)
        assert e.value.code == "sha256_mismatch"
        assert not (images_dir / "a.img").exists()
        assert _temps(images_dir) == []
        assert db.execute_one("SELECT * FROM images") is None

    def test_sha256_match_accepted_any_case(self, store):
        digest = hashlib.sha256(b"data").hexdigest().upper()
        _put(store, "a.img", b"data", BOT, expected_sha256=f" {digest} ")

    @pytest.mark.parametrize("bad", ["xyz", "0" * 63, "g" * 64])
    def test_invalid_sha256_refused_up_front(self, store, images_dir, bad):
        with pytest.raises(ImageError) as e:
            store.begin_upload("a.img", 1, BOT, max_bytes=9, expected_sha256=bad)
        assert e.value.code == "invalid_sha256"
        assert _temps(images_dir) == []

    def test_too_large_refused_up_front(self, store, images_dir):
        with pytest.raises(ImageError) as e:
            store.begin_upload("a.img", 11, BOT, max_bytes=10)
        assert e.value.code == "too_large" and e.value.status == 413
        assert _temps(images_dir) == []

    def test_short_body_stores_nothing(self, store, images_dir):
        upload = store.begin_upload("a.img", 10, BOT, max_bytes=99)
        upload.write(b"short")
        with pytest.raises(ImageError) as e:
            upload.commit()
        assert e.value.code == "length_mismatch"
        assert not (images_dir / "a.img").exists()
        assert _temps(images_dir) == []

    def test_long_body_refused(self, store, images_dir):
        upload = store.begin_upload("a.img", 2, BOT, max_bytes=99)
        with pytest.raises(ImageError):
            upload.write(b"toolong")
        upload.abort()
        assert _temps(images_dir) == []

    def test_insufficient_space(self, store, images_dir):
        with patch("labctl.core.images.FREE_SPACE_RESERVE_BYTES", 1 << 62):
            with pytest.raises(ImageError) as e:
                store.begin_upload("a.img", 1, BOT, max_bytes=99)
        assert e.value.code == "insufficient_space" and e.value.status == 507
        assert _temps(images_dir) == []

    def test_invalid_name_refused(self, store, images_dir):
        with pytest.raises(ImageError) as e:
            store.begin_upload("../evil", 1, BOT, max_bytes=9)
        assert e.value.code == "invalid_name"
        assert os.listdir(images_dir) == []

    def test_symlink_in_place_of_image_not_followed(self, store, images_dir, tmp_path):
        target = tmp_path / "outside"
        target.write_bytes(b"keep")
        (images_dir / "a.img").symlink_to(target)
        with pytest.raises(ImageError) as e:
            _put(store, "a.img", b"x", ALICE, overwrite=True)
        assert e.value.code == "not_a_file"
        assert target.read_bytes() == b"keep"

    def test_symlinked_upload_dir_resolved_like_tool_paths(self, tmp_path, db):
        """The configured path is resolved once, as tools resolve host
        paths (e.g. /srv -> /data/srv); nothing below it is followed."""
        real = tmp_path / "data" / "images"
        real.mkdir(parents=True)
        (tmp_path / "srv").symlink_to(tmp_path / "data")
        store = ImageStore(str(tmp_path / "srv" / "images"), db)
        _put(store, "a.img", b"x", BOT)
        assert (real / "a.img").read_bytes() == b"x"
        assert [i["name"] for i in store.list()] == ["a.img"]

    def test_missing_upload_dir(self, tmp_path, db):
        store = ImageStore(str(tmp_path / "nope"), db)
        with pytest.raises(ImageError) as e:
            store.begin_upload("a.img", 1, BOT, max_bytes=9)
        assert e.value.code == "upload_dir_unavailable"

    def test_concurrent_uploads_share_the_free_space(self, store, images_dir):
        """Each upload counts what the others still have to write."""
        free = 10 << 30
        fake = os.statvfs_result((4096, 1, free, free, free, 0, 0, 0, 0, 255))
        with patch("labctl.core.images.os.statvfs", return_value=fake):
            first = store.begin_upload("a.img", 6 << 30, BOT, max_bytes=1 << 40)
            try:
                with pytest.raises(ImageError) as e:
                    store.begin_upload("b.img", 6 << 30, BOT2, max_bytes=1 << 40)
                assert e.value.code == "insufficient_space"
            finally:
                first.abort()
            # Once it's gone, there is room again.
            store.begin_upload("b.img", 6 << 30, BOT2, max_bytes=1 << 40).abort()
        assert _temps(images_dir) == []

    def test_disk_full_mid_upload_is_507(self, store, images_dir):
        import errno

        upload = store.begin_upload("a.img", 4, BOT, max_bytes=99)
        try:
            with patch(
                "labctl.core.images.os.write",
                side_effect=OSError(errno.ENOSPC, "No space left on device"),
            ):
                with pytest.raises(ImageError) as e:
                    upload.write(b"data")
            assert e.value.code == "insufficient_space" and e.value.status == 507
        finally:
            upload.abort()
        assert os.listdir(images_dir) == []

    def test_record_failure_stores_nothing(self, store, images_dir):
        """The record and the rename are one step: no record, no image."""
        import sqlite3

        upload = store.begin_upload("a.img", 1, BOT, max_bytes=9)
        upload.write(b"x")

        def broken():
            raise sqlite3.OperationalError("database is locked")

        with patch.object(store.db, "connect", side_effect=broken):
            with pytest.raises(sqlite3.OperationalError):
                upload.commit()
        assert os.listdir(images_dir) == []

    def test_rename_failure_rolls_back_record(self, store, images_dir, db):
        _put(store, "a.img", b"one", BOT)
        upload = store.begin_upload("a.img", 3, BOT, overwrite=True, max_bytes=9)
        upload.write(b"two")
        with patch("labctl.core.images.os.rename", side_effect=OSError("boom")):
            with pytest.raises(OSError):
                upload.commit()
        (image,) = store.list()
        assert image["uploaded_by"] == "bot"
        assert image["sha256"] == hashlib.sha256(b"one").hexdigest()

    def test_stale_temps_removed(self, store, images_dir):
        stale = images_dir / f"{TEMP_PREFIX}old"
        fresh = images_dir / f"{TEMP_PREFIX}new"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"x")
        old = time.time() - 2 * 24 * 3600
        os.utime(stale, (old, old))
        _put(store, "a.img", b"x", BOT)
        assert not stale.exists() and fresh.exists()

    def test_reader_of_old_image_keeps_reading_it(self, store, images_dir):
        _put(store, "a.img", b"old", BOT)
        fd = os.open(images_dir / "a.img", os.O_RDONLY)
        try:
            _put(store, "a.img", b"new", BOT, overwrite=True)
            assert os.read(fd, 10) == b"old"
        finally:
            os.close(fd)


class TestStoreListDelete:
    def test_list(self, store, images_dir):
        _put(store, "b.img", b"bb", BOT)
        (images_dir / "a.img").write_bytes(b"a")
        (images_dir / f"{TEMP_PREFIX}x").write_bytes(b"t")
        (images_dir / "sub").mkdir()
        images = store.list()
        assert [i["name"] for i in images] == ["a.img", "b.img"]
        assert images[0]["uploaded_by"] is None and images[0]["sha256"] is None
        assert images[1]["uploaded_by"] == "bot"
        assert images[1]["sha256"] == hashlib.sha256(b"bb").hexdigest()
        assert images[1]["path"] == str(images_dir / "b.img")

    def test_record_of_file_replaced_another_way_gives_no_rights(
        self, store, images_dir
    ):
        """Same size, same name, but not the uploaded file: operators only."""
        _put(store, "a.img", b"one", BOT)
        os.unlink(images_dir / "a.img")
        (images_dir / "a.img").write_bytes(b"two")
        for act in (
            lambda: store.delete("a.img", BOT),
            lambda: _put(store, "a.img", b"new", BOT, overwrite=True),
        ):
            with pytest.raises(ImageError) as e:
                act()
            assert e.value.code == "not_owner"
        assert (images_dir / "a.img").read_bytes() == b"two"
        (image,) = store.list()
        assert image["uploaded_by"] is None

    def test_record_survives_own_replace(self, store):
        _put(store, "a.img", b"one", BOT)
        _put(store, "a.img", b"two", BOT, overwrite=True)
        (image,) = store.list()
        assert image["uploaded_by"] == "bot"
        store.delete("a.img", BOT)

    def test_list_drops_record_of_file_replaced_another_way(self, store, images_dir):
        _put(store, "a.img", b"one", BOT)
        (images_dir / "a.img").write_bytes(b"replaced by scp")
        (image,) = store.list()
        assert image["uploaded_by"] is None and image["sha256"] is None

    def test_delete_own(self, store, images_dir, db):
        _put(store, "a.img", b"x", BOT)
        store.delete("a.img", BOT)
        assert not (images_dir / "a.img").exists()
        assert db.execute_one("SELECT * FROM images") is None

    def test_delete_others_refused(self, store, images_dir):
        _put(store, "a.img", b"x", BOT)
        with pytest.raises(ImageError) as e:
            store.delete("a.img", BOT2)
        assert e.value.code == "not_owner"
        assert (images_dir / "a.img").exists()

    def test_operator_deletes_any(self, store, images_dir):
        (images_dir / "a.img").write_bytes(b"scp")
        store.delete("a.img", ALICE)
        assert not (images_dir / "a.img").exists()

    def test_failed_unlink_keeps_record(self, store, images_dir):
        _put(store, "a.img", b"x", BOT)
        with patch(
            "labctl.core.images.os.unlink", side_effect=PermissionError(13, "denied")
        ):
            with pytest.raises(ImageError) as e:
                store.delete("a.img", BOT)
        assert e.value.code == "delete_failed"
        (image,) = store.list()
        assert image["uploaded_by"] == "bot"

    def test_delete_missing(self, store):
        with pytest.raises(ImageError) as e:
            store.delete("nope.img", ALICE)
        assert e.value.code == "not_found"

    def test_delete_invalid_name(self, store, tmp_path):
        (tmp_path / "victim").write_bytes(b"x")
        with pytest.raises(ImageError) as e:
            store.delete("../victim", ALICE)
        assert e.value.code == "invalid_name"
        assert (tmp_path / "victim").exists()


class TestSchema:
    def test_v9_database_gets_images_table(self, tmp_path):
        import sqlite3

        from labctl.core.database import SCHEMA_VERSION, Database

        path = tmp_path / "old.db"
        db = get_database(path)
        with db.connect() as conn:
            conn.execute("DROP TABLE images")
            conn.execute("DELETE FROM schema_version")
            conn.execute("INSERT INTO schema_version (version) VALUES (9)")
        upgraded = Database(path)
        upgraded.initialize()
        assert SCHEMA_VERSION == 10
        upgraded.execute_insert(
            "INSERT INTO images (name, uploaded_by, size, sha256) VALUES (?,?,?,?)",
            ("a.img", "bot", 1, "0" * 64),
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute_insert(
                "INSERT INTO images (name, size, sha256) VALUES (?,?,?)",
                ("a.img", 1, "0" * 64),
            )


class TestConfig:
    def test_defaults(self):
        mcp = Config.from_dict({}).mcp
        assert mcp.upload_dir == ""
        assert mcp.max_upload_mb == 8192
        assert mcp.cert_file == "" and mcp.key_file == ""

    def test_values_round_trip(self):
        config = Config.from_dict(
            {
                "mcp": {
                    "upload_dir": "/var/lib/labctl/images",
                    "max_upload_mb": 100,
                    "cert_file": "/etc/labctl/cert.pem",
                    "key_file": "/etc/labctl/key.pem",
                }
            }
        )
        assert config.mcp.upload_dir == "/var/lib/labctl/images"
        assert config.mcp.max_upload_mb == 100
        again = Config.from_dict(config.to_dict())
        assert again.mcp == config.mcp

    @pytest.mark.parametrize("value", ["images", 5, ["/x"]])
    def test_bad_upload_dir_ignored(self, value, caplog):
        assert Config.from_dict({"mcp": {"upload_dir": value}}).mcp.upload_dir == ""
        assert "upload_dir" in caplog.text

    @pytest.mark.parametrize("value", [0, -1, "100", 1.5, True])
    def test_bad_max_upload_keeps_default(self, value, caplog):
        mcp = Config.from_dict({"mcp": {"max_upload_mb": value}}).mcp
        assert mcp.max_upload_mb == 8192
        assert "max_upload_mb" in caplog.text


# ---------------------------------------------------------------------------
# MCP: tools, bare names, the HTTP route
# ---------------------------------------------------------------------------


def _users():
    return [
        UserConfig(username="alice", api_key=ALICE_KEY),
        UserConfig(username="bot", api_key=BOT_KEY, role="agent"),
        UserConfig(username="bot2", api_key=BOT2_KEY, role="agent"),
    ]


@pytest.fixture
def lab(tmp_path, images_dir):
    """Config and manager for MCP code: uploads into images_dir, which is
    the read allowlist; auth on with an operator and two agents."""
    from labctl.core.manager import get_manager

    config = Config()
    config.database_path = tmp_path / "lab.db"
    config.auth = AuthConfig(enabled=True, users=_users())
    config.mcp.upload_dir = str(images_dir)
    config.mcp.allowed_read_paths = [str(images_dir)]
    config.mcp.max_upload_mb = 1
    manager = get_manager(config.database_path)
    from labctl import mcp_server

    with (
        patch.object(mcp_server, "_get_config", return_value=config),
        patch.object(mcp_server, "_fresh_config", return_value=config),
        patch.object(mcp_server, "_get_manager", return_value=manager),
    ):
        yield config, manager


def _as(user, role):
    """Run MCP code as an authenticated HTTP user (the token contextvar)."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    token = AccessToken(token="t", client_id=user, scopes=[role])
    return auth_context_var.set(AuthenticatedUser(token))


@pytest.fixture
def as_bot():
    from mcp.server.auth.middleware.auth_context import auth_context_var

    reset = _as("bot", "agent")
    yield
    auth_context_var.reset(reset)


class TestBareNames:
    def test_bare_name_resolves_into_upload_dir(self, lab, images_dir):
        from labctl import mcp_server

        (images_dir / "a.img").write_bytes(b"data")
        fd, err = mcp_server._open_host_read("a.img")
        assert err is None
        try:
            assert os.read(fd, 10) == b"data"
        finally:
            os.close(fd)

    def test_copy_spec_with_bare_source(self, lab, images_dir):
        from labctl import mcp_server

        (images_dir / "zImage").write_bytes(b"k")
        pairs, names, err = mcp_server._open_copy_sources(["zImage:boot/zImage"])
        assert err is None and names == ["zImage"]
        assert pairs[0][1] == "boot/zImage"
        mcp_server._close_fds(pairs)

    @pytest.mark.parametrize("name", ["..", ".upload-x", "a b"])
    def test_invalid_bare_names_refused(self, lab, name):
        from labctl import mcp_server

        fd, err = mcp_server._open_host_read(name)
        assert fd is None and "invalid image name" in err

    def test_bare_name_without_upload_dir(self, lab):
        from labctl import mcp_server

        lab[0].mcp.upload_dir = ""
        fd, err = mcp_server._open_host_read("a.img")
        assert fd is None and "mcp.upload_dir" in err

    def test_upload_dir_outside_allowlist_still_refused(self, lab, images_dir):
        from labctl import mcp_server

        (images_dir / "a.img").write_bytes(b"x")
        lab[0].mcp.allowed_read_paths = ["/nonexistent-root"]
        fd, err = mcp_server._open_host_read("a.img")
        assert fd is None and "outside mcp.allowed_read_paths" in err

    def test_upload_in_progress_unreadable_by_path(self, lab, images_dir):
        """Temp files are unfinished and unverified."""
        from labctl import mcp_server

        store = ImageStore(str(images_dir), lab[1].db)
        upload = store.begin_upload("a.img", 9, BOT, max_bytes=99)
        try:
            upload.write(b"half")
            fd, err = mcp_server._open_host_read(str(images_dir / upload.temp))
            assert fd is None and "dot files in mcp.upload_dir" in err
        finally:
            upload.abort()

    def test_tools_never_write_into_upload_dir(self, lab, images_dir):
        """boot_test's output_dir would truncate uploaded images in place."""
        from labctl import mcp_server

        lab[0].mcp.allowed_write_paths = [str(images_dir.parent)]
        for path in (images_dir, images_dir / "sub"):
            fd, _, err = mcp_server._open_host_write_dir(str(path))
            assert fd is None and "inside mcp.upload_dir" in err
        assert os.listdir(images_dir) == []
        fd, _, err = mcp_server._open_host_write_dir(str(images_dir.parent / "out"))
        assert err is None
        os.close(fd)

    def test_upload_dir_under_another_name_refused(self, lab, images_dir, tmp_path):
        """A bind mount or case alias passes the path check; the directory
        itself is compared too. (A hard-to-make alias is simulated by
        pointing upload_dir at a symlink whose target is the write path.)"""
        from labctl import mcp_server

        out = tmp_path / "out"
        out.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(out)
        lab[0].mcp.upload_dir = str(alias)  # realpath -> out, but checked by inode
        lab[0].mcp.allowed_write_paths = [str(out)]
        with patch.object(mcp_server, "_in_upload_dir", return_value=False):
            fd, _, err = mcp_server._open_host_write_dir(str(out))
        assert fd is None and "inside mcp.upload_dir" in err

    def test_absolute_paths_unchanged(self, lab, images_dir):
        from labctl import mcp_server

        (images_dir / "a.img").write_bytes(b"x")
        fd, err = mcp_server._open_host_read(str(images_dir / "a.img"))
        assert err is None
        os.close(fd)


class TestTools:
    def test_list_images(self, lab, images_dir, as_bot):
        from labctl import mcp_server

        (images_dir / "a.img").write_bytes(b"x")
        data = json.loads(mcp_server.list_images())
        assert [i["name"] for i in data["images"]] == ["a.img"]
        assert data["max_upload_mb"] == 1
        assert "upload" in data

    def test_upload_instructions_over_http(self, lab, as_bot):
        from labctl import mcp_server

        with (
            patch.object(mcp_server, "_SESSION_KIND", "mcp-http"),
            patch.object(mcp_server.mcp, "_token_verifier", object()),
        ):
            upload = json.loads(mcp_server.list_images())["upload"]
        assert upload["available"] is True and upload["method"] == "PUT"
        assert "Authorization: Bearer" in upload["example"]

    def test_upload_instructions_over_stdio(self, lab, as_bot):
        from labctl import mcp_server

        with patch.object(mcp_server, "_SESSION_KIND", "mcp-stdio"):
            upload = json.loads(mcp_server.list_images())["upload"]
        assert upload["available"] is False

    def test_uploads_off(self, lab):
        from labctl import mcp_server

        lab[0].mcp.upload_dir = ""
        assert "uploads are off" in mcp_server.list_images()
        lab[0].mcp.confirm_destructive = False
        assert "uploads are off" in mcp_server.delete_image("a.img")

    def test_delete_image_needs_confirmation(self, lab, images_dir, as_bot):
        from labctl import mcp_server

        store = ImageStore(str(images_dir), lab[1].db)
        _put(store, "a.img", b"x", BOT)
        plan = json.loads(mcp_server.delete_image("a.img"))
        assert plan["status"] == "confirmation_required"
        assert (images_dir / "a.img").exists()
        result = mcp_server.delete_image("a.img", confirm_token=plan["confirm_token"])
        assert result == "Deleted image: a.img"
        assert not (images_dir / "a.img").exists()
        row = lab[1].db.execute_one(
            "SELECT actor, result FROM audit_log WHERE action = 'image_delete'"
        )
        assert row["result"] == "ok" and row["actor"].endswith(":bot")

    def test_delete_others_image_refused_and_logged(self, lab, images_dir, as_bot):
        from labctl import mcp_server

        lab[0].mcp.confirm_destructive = False
        store = ImageStore(str(images_dir), lab[1].db)
        _put(store, "a.img", b"x", BOT2)
        assert "only its uploader or an operator" in mcp_server.delete_image("a.img")
        assert (images_dir / "a.img").exists()
        row = lab[1].db.execute_one(
            "SELECT result FROM audit_log WHERE action = 'image_delete'"
        )
        assert row["result"] == "forbidden"

    def test_delete_unexpected_error_reported_and_audited(
        self, lab, images_dir, as_bot
    ):
        import sqlite3

        from labctl import mcp_server

        lab[0].mcp.confirm_destructive = False
        with patch.object(
            ImageStore, "delete", side_effect=sqlite3.OperationalError("locked")
        ):
            result = mcp_server.delete_image("a.img")
        assert result.startswith("Error: could not delete image 'a.img'")
        row = lab[1].db.execute_one(
            "SELECT result, details FROM audit_log WHERE action = 'image_delete'"
        )
        assert row["result"] == "error" and "OperationalError" in row["details"]

    def test_annotations(self):
        from labctl.mcp_server import TOOL_ANNOTATIONS

        assert TOOL_ANNOTATIONS["list_images"].read_only_hint is True
        assert TOOL_ANNOTATIONS["delete_image"].destructive_hint is True


@pytest.fixture
def http_restore():
    from labctl import mcp_server

    saved = (
        mcp_server.mcp.settings.auth,
        mcp_server.mcp._token_verifier,
        mcp_server._http_security,
    )
    yield
    (
        mcp_server.mcp.settings.auth,
        mcp_server.mcp._token_verifier,
        mcp_server._http_security,
    ) = saved


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def serve(lab, http_restore):
    """Start the server as `labctl mcp --http` does (run_server, then the
    uvicorn server _serve_http would run) on a loopback port; returns a
    function taking ``auth``, ``tls`` and ``allowed_hosts`` and giving
    the base URL."""
    from labctl import mcp_server

    servers = []

    def start(auth=True, tls=None, allowed_hosts=None):
        config = lab[0]
        config.auth.enabled = auth
        config.mcp.allowed_hosts = list(allowed_hosts or [])
        config.mcp.cert_file, config.mcp.key_file = tls or ("", "")
        mcp_server.mcp.settings.auth = None
        mcp_server.mcp._token_verifier = None
        port = _free_port()
        with (
            patch.object(mcp_server, "_serve_http") as serve_http,
            patch.object(mcp_server, "_start_expiry_thread"),
            patch("atexit.register"),
        ):
            mcp_server.run_server(transport="http", http_port=port, host="127.0.0.1")
        server = mcp_server._http_server(*serve_http.call_args.args)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        servers.append((server, thread))
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.02)
        return f"{'https' if tls else 'http'}://127.0.0.1:{port}"

    yield start
    for server, thread in servers:
        server.should_exit = True
        thread.join(timeout=10)


def _request(url, method="PUT", data=b"", headers=None, context=None):
    request = urllib.request.Request(
        url, data=data, method=method, headers=dict(headers or {})
    )
    try:
        with urllib.request.urlopen(request, timeout=10, context=context) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except ValueError:
            return e.code, body.decode()


def _bearer(key):
    return {"Authorization": f"Bearer {key}"}


class TestUploadRoute:
    def test_upload_as_agent(self, serve, images_dir, lab):
        base = serve()
        data = b"image-bytes" * 1000
        status, body = _request(
            f"{base}/images/a.img", data=data, headers=_bearer(BOT_KEY)
        )
        assert status == 201, body
        assert body["sha256"] == hashlib.sha256(data).hexdigest()
        assert body["uploaded_by"] == "bot"
        assert (images_dir / "a.img").read_bytes() == data
        row = lab[1].db.execute_one(
            "SELECT actor, source, result, entity_name FROM audit_log"
            " WHERE action = 'image_upload'"
        )
        assert row["result"] == "ok" and row["source"] == "mcp"
        assert row["actor"].endswith(":bot") and row["entity_name"] == "a.img"

    def test_large_upload_spans_writes(self, serve, images_dir):
        base = serve()
        data = os.urandom(900 * 1024)
        with patch("labctl.mcp_server._UPLOAD_WRITE_BYTES", 64 * 1024):
            status, body = _request(
                f"{base}/images/big.img", data=data, headers=_bearer(BOT_KEY)
            )
        assert status == 201, body
        assert (images_dir / "big.img").read_bytes() == data

    @pytest.mark.parametrize("key", [None, "wrong", BOT_KEY[:-1]])
    def test_needs_valid_key(self, serve, images_dir, key):
        base = serve()
        headers = _bearer(key) if key else {}
        status, _ = _request(f"{base}/images/a.img", data=b"x", headers=headers)
        assert status == 401
        assert os.listdir(images_dir) == []

    def test_no_auth_server_accepts_anonymous_operator(self, serve, images_dir):
        base = serve(auth=False)
        status, body = _request(f"{base}/images/a.img", data=b"x")
        assert status == 201 and body["uploaded_by"] is None

    def test_foreign_host_header_refused(self, serve, images_dir):
        """The SDK's DNS-rebinding check covers /mcp only; the route
        applies it too."""
        base = serve(auth=False)
        status, _ = _request(
            f"{base}/images/a.img", data=b"x", headers={"Host": "evil.example"}
        )
        assert status == 421
        assert os.listdir(images_dir) == []

    def test_configured_host_header_accepted(self, serve):
        base = serve(allowed_hosts=["lab.example.com"])
        headers = {**_bearer(BOT_KEY), "Host": "lab.example.com"}
        status, _ = _request(f"{base}/images/a.img", data=b"x", headers=headers)
        assert status == 201

    def test_other_agent_cannot_replace(self, serve, images_dir, lab):
        base = serve()
        _request(f"{base}/images/a.img", data=b"one", headers=_bearer(BOT_KEY))
        status, body = _request(
            f"{base}/images/a.img?overwrite=1", data=b"two", headers=_bearer(BOT2_KEY)
        )
        assert status == 403 and body["error"] == "not_owner"
        assert (images_dir / "a.img").read_bytes() == b"one"
        row = lab[1].db.execute_one(
            "SELECT result, actor FROM audit_log WHERE action = 'image_upload'"
            " ORDER BY id DESC LIMIT 1"
        )
        assert row["result"] == "forbidden" and row["actor"].endswith(":bot2")

    def test_overwrite_needs_flag(self, serve, images_dir):
        base = serve()
        _request(f"{base}/images/a.img", data=b"one", headers=_bearer(BOT_KEY))
        status, body = _request(
            f"{base}/images/a.img", data=b"two", headers=_bearer(BOT_KEY)
        )
        assert status == 409 and body["error"] == "exists"
        status, body = _request(
            f"{base}/images/a.img?overwrite=true", data=b"two", headers=_bearer(BOT_KEY)
        )
        assert status == 200 and body["replaced"] is True
        assert (images_dir / "a.img").read_bytes() == b"two"

    def test_operator_replaces_agents_image(self, serve, images_dir):
        base = serve()
        _request(f"{base}/images/a.img", data=b"one", headers=_bearer(BOT_KEY))
        status, _ = _request(
            f"{base}/images/a.img?overwrite=1", data=b"two", headers=_bearer(ALICE_KEY)
        )
        assert status == 200

    def test_sha256_mismatch(self, serve, images_dir):
        base = serve()
        status, body = _request(
            f"{base}/images/a.img?sha256={'0' * 64}",
            data=b"x",
            headers=_bearer(BOT_KEY),
        )
        assert status == 422 and body["error"] == "sha256_mismatch"
        assert os.listdir(images_dir) == []

    def test_sha256_match(self, serve):
        base = serve()
        digest = hashlib.sha256(b"x").hexdigest()
        status, _ = _request(
            f"{base}/images/a.img?sha256={digest}", data=b"x", headers=_bearer(BOT_KEY)
        )
        assert status == 201

    def test_over_limit_refused_before_body(self, serve, images_dir):
        base = serve()  # max_upload_mb = 1
        status, body = _request(
            f"{base}/images/a.img",
            data=b"x" * ((1 << 20) + 1),
            headers=_bearer(BOT_KEY),
        )
        assert status == 413 and body["error"] == "too_large"
        assert os.listdir(images_dir) == []

    @pytest.mark.parametrize("name", [".hidden", "a%20b", "%2E%2E"])
    def test_invalid_names(self, serve, images_dir, name):
        base = serve()
        status, _ = _request(
            f"{base}/images/{name}", data=b"x", headers=_bearer(BOT_KEY)
        )
        assert status in (400, 404)
        assert os.listdir(images_dir) == []

    def test_path_traversal_not_routed(self, serve, images_dir, tmp_path):
        base = serve()
        status, _ = _request(
            f"{base}/images/..%2Fescape", data=b"x", headers=_bearer(BOT_KEY)
        )
        assert status in (400, 404)
        assert not (tmp_path / "escape").exists()

    def test_chunked_without_length_refused(self, serve, images_dir):
        base = serve()

        def body():
            yield b"x"

        request = urllib.request.Request(
            f"{base}/images/a.img",
            data=body(),
            method="PUT",
            headers={**_bearer(BOT_KEY), "Transfer-Encoding": "chunked"},
        )
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(request, timeout=10)
        assert e.value.code == 411
        assert os.listdir(images_dir) == []

    def test_unexpected_error_is_json_500_and_audited(self, serve, lab, images_dir):
        import sqlite3

        base = serve()
        with patch.object(
            ImageStore,
            "begin_upload",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            status, body = _request(
                f"{base}/images/a.img", data=b"x", headers=_bearer(BOT_KEY)
            )
        assert status == 500 and body["error"] == "upload_failed"
        row = lab[1].db.execute_one(
            "SELECT result, details FROM audit_log WHERE action = 'image_upload'"
        )
        assert row["result"] == "error" and "OperationalError" in row["details"]

    def test_setup_failure_is_json_500(self, serve, images_dir):
        from labctl import mcp_server

        base = serve()
        with patch.object(
            mcp_server, "_image_store", side_effect=RuntimeError("db unavailable")
        ):
            status, body = _request(
                f"{base}/images/a.img", data=b"x", headers=_bearer(BOT_KEY)
            )
        assert status == 500 and body["error"] == "upload_failed"
        assert os.listdir(images_dir) == []

    def test_uploads_off(self, serve, lab, images_dir):
        base = serve()
        lab[0].mcp.upload_dir = ""
        status, body = _request(
            f"{base}/images/a.img", data=b"x", headers=_bearer(BOT_KEY)
        )
        assert status == 403 and body["error"] == "uploads_disabled"

    def test_only_put(self, serve):
        base = serve()
        status, _ = _request(
            f"{base}/images/a.img", method="GET", data=None, headers=_bearer(BOT_KEY)
        )
        assert status == 405

    def test_uploaded_image_usable_by_bare_name(self, serve, lab, images_dir):
        from labctl import mcp_server

        base = serve()
        _request(f"{base}/images/a.img", data=b"payload", headers=_bearer(BOT_KEY))
        fd, err = mcp_server._open_host_read("a.img")
        assert err is None
        try:
            assert os.read(fd, 20) == b"payload"
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# TLS
# ---------------------------------------------------------------------------


@pytest.fixture
def cert_pair(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    return str(cert), str(key)


class TestTls:
    def test_none_configured(self):
        from labctl.mcp_server import http_tls_files

        assert http_tls_files(Config()) is None

    def test_flags_override_config(self, cert_pair, tmp_path):
        from labctl.mcp_server import http_tls_files

        config = Config()
        config.mcp.cert_file = str(tmp_path / "missing-cert")
        config.mcp.key_file = str(tmp_path / "missing-key")
        assert http_tls_files(config, *cert_pair) == cert_pair

    def test_from_config(self, cert_pair):
        from labctl.mcp_server import http_tls_files

        config = Config()
        config.mcp.cert_file, config.mcp.key_file = cert_pair
        assert http_tls_files(config) == cert_pair

    def test_one_without_the_other_refused(self, cert_pair):
        from labctl.mcp_server import McpStartupError, http_tls_files

        with pytest.raises(McpStartupError, match="both"):
            http_tls_files(Config(), cert_pair[0], None)

    def test_mismatched_pair_refused(self, cert_pair, tmp_path):
        from labctl.mcp_server import McpStartupError, http_tls_files

        other = tmp_path / "other"
        other.mkdir()
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=x",
                "-keyout",
                str(other / "key.pem"),
                "-out",
                str(other / "cert.pem"),
            ],
            check=True,
            capture_output=True,
        )
        with pytest.raises(McpStartupError, match="don't load as a pair"):
            http_tls_files(Config(), cert_pair[0], str(other / "key.pem"))

    def test_garbage_pem_refused(self, cert_pair, tmp_path):
        from labctl.mcp_server import McpStartupError, http_tls_files

        junk = tmp_path / "junk.pem"
        junk.write_text("not a certificate\n")
        with pytest.raises(McpStartupError, match="don't load as a pair"):
            http_tls_files(Config(), str(junk), cert_pair[1])

    def test_upload_example_follows_scheme(self, serve, cert_pair, lab):
        from labctl import mcp_server

        with patch.object(mcp_server, "_SESSION_KIND", "mcp-http"):
            serve()
            assert '"http://' in mcp_server._upload_instructions()["example"]
            serve(tls=cert_pair)
            assert '"https://' in mcp_server._upload_instructions()["example"]

    def test_unreadable_refused(self, cert_pair, tmp_path):
        from labctl.mcp_server import McpStartupError, http_tls_files

        with pytest.raises(McpStartupError, match="can't be read"):
            http_tls_files(Config(), str(tmp_path / "nope"), cert_pair[1])

    def test_serves_https(self, serve, cert_pair, images_dir):
        base = serve(tls=cert_pair)
        assert base.startswith("https://")
        context = ssl.create_default_context(cafile=cert_pair[0])
        status, body = _request(
            f"{base}/images/a.img",
            data=b"secure",
            headers=_bearer(BOT_KEY),
            context=context,
        )
        assert status == 201, body
        assert (images_dir / "a.img").read_bytes() == b"secure"

    def test_run_server_passes_tls_to_uvicorn(self, cert_pair, http_restore, capsys):
        from labctl import mcp_server

        config = Config()
        config.auth = AuthConfig(enabled=True, users=_users())
        config.mcp.cert_file, config.mcp.key_file = cert_pair
        with (
            patch.object(mcp_server, "_get_config", return_value=config),
            patch.object(mcp_server, "_start_expiry_thread"),
            patch("atexit.register"),
            patch("uvicorn.Server") as server,
        ):
            mcp_server.run_server(transport="http", host="127.0.0.1", http_port=9)
        uv_config = server.call_args.args[0]
        assert uv_config.ssl_certfile == cert_pair[0]
        assert uv_config.ssl_keyfile == cert_pair[1]
        server.return_value.run.assert_called_once()
        assert "MCP HTTP: TLS with certificate" in capsys.readouterr().out
        assert mcp_server._http_security is not mcp_server._UNSET

    def test_plain_http_beyond_loopback_warns(self, caplog):
        from labctl import mcp_server

        config = Config()
        config.auth = AuthConfig(enabled=True, users=_users())
        mcp_server.check_http_startup("0.0.0.0", config, None)
        assert "without TLS" in caplog.text

    def test_tls_beyond_loopback_no_warning(self, caplog, cert_pair):
        from labctl import mcp_server

        config = Config()
        config.auth = AuthConfig(enabled=True, users=_users())
        mcp_server.check_http_startup("0.0.0.0", config, cert_pair)
        assert "without TLS" not in caplog.text


class TestStartupChecks:
    def test_upload_dir_outside_read_allowlist_warns(self, caplog, images_dir):
        from labctl import mcp_server

        config = Config()
        config.mcp.upload_dir = str(images_dir)
        mcp_server.check_http_startup("127.0.0.1", config)
        assert "outside mcp.allowed_read_paths" in caplog.text

    def test_missing_upload_dir_warns(self, caplog, tmp_path):
        from labctl import mcp_server

        config = Config()
        config.mcp.upload_dir = str(tmp_path / "nope")
        config.mcp.allowed_read_paths = [str(tmp_path)]
        mcp_server.check_http_startup("127.0.0.1", config)
        assert "is not a directory" in caplog.text

    def test_good_upload_dir_quiet(self, caplog, images_dir):
        from labctl import mcp_server

        config = Config()
        config.mcp.upload_dir = str(images_dir)
        config.mcp.allowed_read_paths = [str(images_dir.parent)]
        mcp_server.check_http_startup("127.0.0.1", config)
        assert "upload_dir" not in caplog.text


class TestCli:
    @pytest.fixture(autouse=True)
    def _restore_config_env(self, monkeypatch):
        for name in ("LABCTL_CONFIG", "LABCTL_CONFIG_EXCLUSIVE"):
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)

    def _config_file(self, tmp_path, extra=""):
        path = tmp_path / "config.yaml"
        path.write_text(
            f"database_path: {tmp_path / 'lab.db'}\n"
            "mcp:\n"
            f"  upload_dir: {tmp_path / 'images'}\n"
            f"  allowed_read_paths: [{tmp_path / 'images'}]\n"
            "  max_upload_mb: 64\n" + extra
        )
        (tmp_path / "images").mkdir(exist_ok=True)
        return path

    def test_check_reports_tls_and_uploads(self, tmp_path, cert_pair):
        from labctl.cli import main

        path = self._config_file(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(path),
                "mcp",
                "--http",
                "8080",
                "--cert",
                cert_pair[0],
                "--key",
                cert_pair[1],
                "--check",
            ],
        )
        assert result.exit_code == 0, result.output
        assert f"tls: {cert_pair[0]}" in result.output
        assert "uploads: PUT /images/<name>" in result.output
        assert "max 64 MiB" in result.output

    def test_check_plain_and_uploads_off(self, tmp_path):
        from labctl.cli import main

        path = tmp_path / "config.yaml"
        path.write_text(f"database_path: {tmp_path / 'lab.db'}\n")
        result = CliRunner().invoke(
            main, ["-c", str(path), "mcp", "--http", "8080", "--check"]
        )
        assert result.exit_code == 0, result.output
        assert "tls: off" in result.output
        assert "uploads: off" in result.output

    def test_check_refuses_half_tls(self, tmp_path, cert_pair):
        from labctl.cli import main

        path = self._config_file(tmp_path)
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(path),
                "mcp",
                "--http",
                "8080",
                "--cert",
                cert_pair[0],
                "--check",
            ],
        )
        assert result.exit_code == 1
        assert "both a certificate and a key" in result.output

    def test_cert_needs_http(self, tmp_path, cert_pair):
        from labctl.cli import main

        path = self._config_file(tmp_path)
        result = CliRunner().invoke(
            main, ["-c", str(path), "mcp", "--cert", cert_pair[0]]
        )
        assert result.exit_code == 2
        assert "--cert/--key only apply with --http" in result.output

    def test_cli_passes_tls_to_run_server(self, tmp_path, cert_pair):
        from labctl.cli import main

        path = self._config_file(tmp_path)
        with patch("labctl.mcp_server.run_server") as run:
            result = CliRunner().invoke(
                main,
                [
                    "-c",
                    str(path),
                    "mcp",
                    "--http",
                    "8443",
                    "--cert",
                    cert_pair[0],
                    "--key",
                    cert_pair[1],
                ],
            )
        assert result.exit_code == 0, result.output
        run.assert_called_once_with(
            transport="http",
            http_port=8443,
            host="127.0.0.1",
            cert=cert_pair[0],
            key=cert_pair[1],
        )

"""labctl.core.admin_files: root-run file operations under directories the
labctl user can write, which must never be redirected by a planted
symlink or hard link."""

import grp
import os
import pwd
import stat

import pytest

from labctl.core import admin_files

USER = pwd.getpwuid(os.getuid()).pw_name
GROUP = grp.getgrgid(os.getgid()).gr_name


@pytest.fixture
def victim(tmp_path):
    """A file outside the managed directory that must stay untouched."""
    path = tmp_path / "victim"
    path.write_text("secret\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def managed(tmp_path):
    path = tmp_path / "managed"
    path.mkdir()
    return path


class TestState:
    def test_missing_and_regular(self, managed):
        assert admin_files.state(str(managed / "config.yaml")) == "missing"
        (managed / "config.yaml").write_text("x")
        assert admin_files.state(str(managed / "config.yaml")) == "regular"

    def test_symlink_refused(self, managed, victim):
        (managed / "config.yaml").symlink_to(victim)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.state(str(managed / "config.yaml"))

    def test_dangling_symlink_refused(self, managed, tmp_path):
        (managed / "config.yaml").symlink_to(tmp_path / "nowhere")
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.state(str(managed / "config.yaml"))

    def test_symlinked_parent_refused(self, managed, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "config.yaml").write_text("x")
        (managed / ".config").symlink_to(real)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.state(str(managed / ".config" / "config.yaml"))

    def test_hard_link_refused(self, managed, victim):
        os.link(victim, managed / "config.yaml")
        with pytest.raises(admin_files.UnsafeFileError, match="hard link"):
            admin_files.state(str(managed / "config.yaml"))

    def test_symlinked_parent_with_missing_target_refused(self, managed, tmp_path):
        """Not "missing": the parent itself is a planted symlink."""
        empty = tmp_path / "empty"
        empty.mkdir()
        (managed / ".config").symlink_to(empty)
        with pytest.raises(admin_files.UnsafeFileError, match="symlink"):
            admin_files.state(str(managed / ".config" / "config.yaml"))

    def test_missing_parent_is_missing(self, managed):
        """No .config/labctl at all (an admin removed the unused copy)."""
        assert admin_files.state(str(managed / "gone" / "config.yaml")) == "missing"

    def test_fifo_refused(self, managed):
        os.mkfifo(managed / "config.yaml")
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.state(str(managed / "config.yaml"))


class TestSecure:
    def test_sets_mode(self, managed):
        path = managed / "config.yaml"
        path.write_text("x")
        path.chmod(0o644)
        admin_files.secure(str(path), USER, GROUP, 0o640)
        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    def test_symlink_not_followed(self, managed, victim):
        (managed / "config.yaml").symlink_to(victim)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.secure(str(managed / "config.yaml"), USER, GROUP, 0o644)
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600


class TestCreate:
    def test_creates_empty_with_mode(self, managed):
        path = managed / "labctl.db"
        admin_files.create(str(path), USER, GROUP, 0o664)
        assert path.read_bytes() == b""
        assert stat.S_IMODE(path.stat().st_mode) == 0o664

    def test_existing_kept_and_secured(self, managed):
        path = managed / "labctl.db"
        path.write_bytes(b"SQLite data")
        path.chmod(0o644)
        admin_files.create(str(path), USER, GROUP, 0o664)
        assert path.read_bytes() == b"SQLite data"
        assert stat.S_IMODE(path.stat().st_mode) == 0o664

    def test_symlink_not_followed(self, managed, victim):
        (managed / "labctl.db").symlink_to(victim)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.create(str(managed / "labctl.db"), USER, GROUP, 0o664)
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600
        assert victim.read_text() == "secret\n"

    def test_dangling_symlink_not_created_through(self, managed, tmp_path):
        target = tmp_path / "elsewhere"
        (managed / "labctl.db").symlink_to(target)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.create(str(managed / "labctl.db"), USER, GROUP, 0o664)
        assert not target.exists()

    def test_hard_link_refused(self, managed, victim):
        os.link(victim, managed / "labctl.db")
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.create(str(managed / "labctl.db"), USER, GROUP, 0o664)
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600

    def test_cli(self, managed):
        path = managed / "labctl.db"
        assert admin_files.main(["create", str(path), USER, GROUP, "664"]) == 0
        assert stat.S_IMODE(path.stat().st_mode) == 0o664


class TestSecureDir:
    def test_creates_with_mode(self, managed):
        admin_files.secure_dir(str(managed / "images"), USER, GROUP, 0o2775)
        assert stat.S_IMODE((managed / "images").stat().st_mode) == 0o2775

    def test_symlinked_dir_not_followed(self, managed, tmp_path):
        target = tmp_path / "etc"
        target.mkdir(mode=0o755)
        (managed / "images").symlink_to(target)
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.secure_dir(str(managed / "images"), USER, GROUP, 0o2777)
        assert stat.S_IMODE(target.stat().st_mode) == 0o755


class TestSecureDirParents:
    def test_missing_parent_not_created(self, managed):
        """Only the leaf is created: a missing labctl home must not be
        rebuilt as a root-owned 0700 chain."""
        home = managed / "home"
        with pytest.raises(FileNotFoundError):
            admin_files.secure_dir(str(home / "images"), USER, GROUP, 0o2775)
        assert not home.exists()

    def test_file_in_path_named_as_such(self, managed):
        (managed / ".config").write_text("stray")
        with pytest.raises(admin_files.UnsafeFileError, match="non-directory"):
            admin_files.state(str(managed / ".config" / "labctl" / "config.yaml"))


class TestPathValidation:
    def test_unnormalized_path_named_as_such(self, managed):
        (managed / "f").write_text("x")
        with pytest.raises(admin_files.UnsafeFileError, match="normalized"):
            admin_files.state(str(managed) + "/../managed/f")


class TestCopy:
    def test_copies_with_mode_and_line_replacement(self, managed, tmp_path):
        src = tmp_path / "example.yaml"
        src.write_text("# database_path: keep\ndatabase_path: ~/old.db\nx: 1\n")
        dst = managed / "config.yaml"
        admin_files.copy(
            str(src), str(dst), 0o640, {"database_path: ~/old.db": "database_path: /n"}
        )
        assert dst.read_text() == "# database_path: keep\ndatabase_path: /n\nx: 1\n"
        assert stat.S_IMODE(dst.stat().st_mode) == 0o640

    def test_symlinked_source_refused(self, managed, victim, tmp_path):
        """Seeding /etc from a service config that points at /etc/shadow."""
        (managed / "config.yaml").symlink_to(victim)
        dst = tmp_path / "etc-config.yaml"
        with pytest.raises(admin_files.UnsafeFileError):
            admin_files.copy(str(managed / "config.yaml"), str(dst), 0o640)
        assert not dst.exists()

    def test_existing_destination_refused(self, managed, victim, tmp_path):
        """A symlink planted at the destination is never written through."""
        src = tmp_path / "example.yaml"
        src.write_text("new\n")
        (managed / "config.yaml").symlink_to(victim)
        with pytest.raises(admin_files.UnsafeFileError, match="already exists"):
            admin_files.copy(str(src), str(managed / "config.yaml"), 0o640)
        assert victim.read_text() == "secret\n"

    def test_symlinked_destination_parent_refused(self, managed, tmp_path):
        src = tmp_path / "example.yaml"
        src.write_text("new\n")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (managed / ".config").symlink_to(elsewhere)
        with pytest.raises(admin_files.UnsafeFileError, match="symlink"):
            admin_files.copy(str(src), str(managed / ".config" / "c.yaml"), 0o640)
        assert not (elsewhere / "c.yaml").exists()

    def test_failed_write_leaves_no_partial_file(self, managed, tmp_path, monkeypatch):
        """ENOSPC mid-copy: no truncated config for a rerun to skip over."""
        import errno as errno_mod

        src = tmp_path / "example.yaml"
        src.write_text("a: 1\n")
        dst = managed / "config.yaml"

        def full(fd):
            raise OSError(errno_mod.ENOSPC, "No space left on device")

        monkeypatch.setattr(admin_files.os, "fsync", full)
        with pytest.raises(OSError):
            admin_files.copy(str(src), str(dst), 0o640)
        assert not dst.exists()

    def test_byte_for_byte(self, managed, tmp_path):
        """CRLF and non-UTF-8 bytes survive, so a later cmp matches."""
        src = tmp_path / "example.yaml"
        src.write_bytes(b"a: 1\r\n# caf\xe9\r\n")
        dst = managed / "config.yaml"
        admin_files.copy(str(src), str(dst), 0o640)
        assert dst.read_bytes() == src.read_bytes()


class TestCli:
    def test_state_exit_codes(self, managed, victim, capsys):
        assert admin_files.main(["state", str(managed / "a")]) == 0
        assert capsys.readouterr().out == "missing\n"
        (managed / "b").symlink_to(victim)
        assert admin_files.main(["state", str(managed / "b")]) == 2
        assert capsys.readouterr().out.startswith("refusing:")

    def test_bad_usage(self):
        assert admin_files.main(["nope"]) == 64
        assert admin_files.main([]) == 64

    def test_error_names_the_requested_path(self, managed, capsys):
        missing = managed / "nohome" / "images"
        rc = admin_files.main(["secure-dir", str(missing), USER, GROUP, "2775"])
        assert rc == 2
        assert str(missing) in capsys.readouterr().err

    def test_errors_are_one_line_not_tracebacks(self, managed, capsys):
        """The scripts show the reason they capture: every failure has one."""
        (managed / "f").write_text("x")
        assert (
            admin_files.main(
                ["secure", str(managed / "f"), "no-such-user-x", GROUP, "640"]
            )
            == 2
        )
        assert "unknown user or group" in capsys.readouterr().err
        assert admin_files.main(["secure", str(managed / "f"), USER, GROUP, "rw"]) == 2
        assert capsys.readouterr().err.startswith("error:")


class TestSharedDb:
    """shared_db: what install-services.sh and update.sh run for the
    services' database. Problems are reported (exit 2), never raised."""

    def _config(self, tmp_path, db_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"database_path: {db_path}\n")
        return cfg

    def test_configured_dir_made_group_writable(self, managed, tmp_path, capsys):
        db_dir = managed / "db"
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert stat.S_IMODE(db_dir.stat().st_mode) == 0o2775
        db = db_dir / "labctl.db"
        assert db.read_bytes() == b""
        assert stat.S_IMODE(db.stat().st_mode) == 0o664
        assert "[ok] Database" in capsys.readouterr().out

    def test_existing_wal_and_shm_secured(self, managed, tmp_path):
        db_dir = managed / "db"
        db_dir.mkdir()
        for name in ("labctl.db", "labctl.db-wal", "labctl.db-shm"):
            (db_dir / name).write_bytes(b"x")
            (db_dir / name).chmod(0o644)
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        for name in ("labctl.db", "labctl.db-wal", "labctl.db-shm"):
            assert stat.S_IMODE((db_dir / name).stat().st_mode) == 0o664

    def test_database_elsewhere_left_alone(self, managed, tmp_path, capsys):
        db_dir = managed / "db"
        cfg = self._config(tmp_path, managed / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert not db_dir.exists()
        assert "Moving the database" in capsys.readouterr().out

    def test_symlinked_dir_left_alone(self, managed, tmp_path, capsys):
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        db_dir = managed / "db"
        db_dir.symlink_to(real)
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert stat.S_IMODE(real.stat().st_mode) == 0o700
        assert not (real / "labctl.db").exists()
        assert "is a symlink" in capsys.readouterr().out

    def test_symlinked_database_warns(self, managed, tmp_path, victim, capsys):
        db_dir = managed / "db"
        db_dir.mkdir()
        (db_dir / "labctl.db").symlink_to(victim)
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 2
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600
        assert "[!!] Database" in capsys.readouterr().out

    def test_non_regular_wal_warns_and_rest_done(self, managed, tmp_path, capsys):
        db_dir = managed / "db"
        db_dir.mkdir()
        os.mkfifo(db_dir / "labctl.db-wal")
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 2
        assert stat.S_IMODE((db_dir / "labctl.db").stat().st_mode) == 0o664
        assert "Left as is" in capsys.readouterr().out

    @pytest.mark.parametrize("body", ["database_path: [unclosed\n", None])
    def test_unreadable_config_warns(self, managed, tmp_path, capsys, body):
        cfg = tmp_path / "config.yaml"
        if body is not None:
            cfg.write_text(body)
        db_dir = managed / "db"
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 2
        assert not db_dir.exists()
        assert "not checked" in capsys.readouterr().out

    def test_cli_exit_code(self, managed, tmp_path):
        db_dir = managed / "db"
        cfg = self._config(tmp_path, db_dir / "labctl.db")
        argv = ["shared-db", str(cfg), str(db_dir), USER, GROUP]
        assert admin_files.main(argv) == 0


class TestSharedDbHomeSearch:
    """shared_db makes sure labctl group members can traverse the labctl
    home (db_dir's parent): g+x only, and a warning when they still can't."""

    def _setup(self, tmp_path, home_mode):
        home = tmp_path / "home"
        home.mkdir()
        home.chmod(home_mode)
        db_dir = home / "db"
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"database_path: {db_dir / 'labctl.db'}\n")
        return home, db_dir, cfg

    def test_adds_only_group_search(self, tmp_path, capsys):
        home, db_dir, cfg = self._setup(tmp_path, 0o700)
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert stat.S_IMODE(home.stat().st_mode) == 0o710  # no g+w, no o+x
        out = capsys.readouterr().out
        assert "added group search (g+x)" in out
        assert "[ok] Database" in out

    def test_keeps_other_bits(self, tmp_path):
        home, db_dir, cfg = self._setup(tmp_path, 0o2750)
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert stat.S_IMODE(home.stat().st_mode) == 0o2750

    def test_other_group_without_search_warns(self, tmp_path, capsys):
        home, db_dir, _ = self._setup(tmp_path, 0o700)
        other_gid = os.getgid() + 54321  # not the home's group
        reason = admin_files._ensure_group_search(str(home), other_gid)
        assert "can't be traversed" in reason
        assert stat.S_IMODE(home.stat().st_mode) == 0o700  # untouched

    def test_other_group_but_world_search_ok(self, tmp_path):
        home, _, _ = self._setup(tmp_path, 0o701)
        assert admin_files._ensure_group_search(str(home), os.getgid() + 54321) is None

    def test_unreachable_home_is_a_warning_not_ok(self, tmp_path, capsys, monkeypatch):
        home, db_dir, cfg = self._setup(tmp_path, 0o700)
        monkeypatch.setattr(
            admin_files, "_ensure_group_search", lambda h, g: f"{h} unreachable"
        )
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 2
        out = capsys.readouterr().out
        assert "[!!]" in out and "unreachable" in out
        assert "[ok] Database" not in out

    def test_symlinked_home_refused_for_chmod(self, tmp_path, monkeypatch):
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        link = tmp_path / "home"
        link.symlink_to(real)
        with pytest.raises(OSError):
            admin_files._ensure_group_search(str(link), os.getgid())
        assert stat.S_IMODE(real.stat().st_mode) == 0o700


class TestSharedDbResolvedPaths:
    """database_path is compared with db_dir resolved, so a symlinked
    /var/lib/labctl still matches; the database file is never followed."""

    def test_path_through_symlinked_home_matches(self, tmp_path):
        real_home = tmp_path / "data" / "labctl"
        real_home.mkdir(parents=True)
        (tmp_path / "varlib").mkdir()
        (tmp_path / "varlib" / "labctl").symlink_to(real_home)
        db_dir = real_home / "db"  # the scripts pass the resolved home
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"database_path: {tmp_path / 'varlib/labctl/db/labctl.db'}\n")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 0
        assert stat.S_IMODE((db_dir / "labctl.db").stat().st_mode) == 0o664

    @pytest.mark.parametrize(
        "configured,expected",
        [
            ("{db}/labctl.db", True),
            ("{db}/../db/labctl.db", True),
            ("{db}/other.db", False),
            ("relative/db/labctl.db", False),
            ("", False),
        ],
    )
    def test_is_db_in(self, tmp_path, configured, expected):
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        assert (
            admin_files._is_db_in(configured.format(db=db_dir), str(db_dir)) is expected
        )

    def test_symlinked_database_file_not_followed(self, tmp_path, victim, capsys):
        """A database_path naming the right file matches by name; a symlink
        planted as that file is refused, not resolved or chmodded."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        (db_dir / "labctl.db").symlink_to(victim)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"database_path: {db_dir / 'labctl.db'}\n")
        assert admin_files.shared_db(str(cfg), str(db_dir), USER, GROUP) == 2
        assert stat.S_IMODE(victim.stat().st_mode) == 0o600

    def test_symlink_named_like_db_elsewhere_doesnt_match(self, tmp_path, victim):
        """database_path pointing at a symlink outside db_dir whose target is
        db_dir/labctl.db is still "elsewhere": the file isn't resolved."""
        db_dir = tmp_path / "db"
        db_dir.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        (other / "labctl.db").symlink_to(db_dir / "labctl.db")
        assert not admin_files._is_db_in(str(other / "labctl.db"), str(db_dir))

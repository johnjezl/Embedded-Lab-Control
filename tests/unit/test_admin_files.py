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

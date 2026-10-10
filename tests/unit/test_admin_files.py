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
        with pytest.raises(FileExistsError):
            admin_files.copy(str(src), str(managed / "config.yaml"), 0o640)
        assert victim.read_text() == "secret\n"


class TestCli:
    def test_state_exit_codes(self, managed, victim, capsys):
        assert admin_files.main(["state", str(managed / "a")]) == 0
        assert capsys.readouterr().out == "missing\n"
        (managed / "b").symlink_to(victim)
        assert admin_files.main(["state", str(managed / "b")]) == 2
        assert capsys.readouterr().out.startswith("refusing:")

    def test_bad_usage(self):
        assert admin_files.main(["nope"]) == 64

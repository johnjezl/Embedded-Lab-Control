"""labctl skill install / list / uninstall (#56)."""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from labctl import claude_skills
from labctl.cli import main

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _run(*args):
    return CliRunner().invoke(main, ["skill", *args])


class TestBundled:
    def test_deploy_and_test_is_bundled(self):
        assert "deploy-and-test" in claude_skills.bundled()
        assert claude_skills.text("deploy-and-test").startswith(b"---\nname:")

    def test_unknown_skill(self):
        with pytest.raises(claude_skills.SkillError, match="no bundled skill"):
            claude_skills.text("nope")

    def test_shipped_in_the_wheel(self):
        """package-data carries every bundled SKILL.md into the wheel."""
        pyproject = (ROOT / "pyproject.toml").read_text()
        section = pyproject.split("[tool.setuptools.package-data]", 1)[1]
        assert '"skills/*/SKILL.md"' in section.split("\n[", 1)[0]
        pkg = ROOT / "src" / "labctl"
        shipped = {p.parent.name for p in pkg.glob("skills/*/SKILL.md")}
        assert shipped == set(claude_skills.bundled())


class TestInstall:
    def test_install_unchanged_force(self, proj, home):
        out = claude_skills.install("deploy-and-test", project=proj)
        path = proj / ".claude" / "skills" / "deploy-and-test" / "SKILL.md"
        assert (out.status, out.path.resolve()) == ("installed", path.resolve())
        assert path.read_bytes() == claude_skills.text("deploy-and-test")
        assert claude_skills.install("deploy-and-test", project=proj).status == (
            "unchanged"
        )
        path.write_text("edited")
        with pytest.raises(claude_skills.SkillError, match="--force"):
            claude_skills.install("deploy-and-test", project=proj)
        assert path.read_text() == "edited"
        out = claude_skills.install("deploy-and-test", project=proj, force=True)
        assert out.status == "updated"
        assert path.read_bytes() == claude_skills.text("deploy-and-test")
        assert not [p for p in path.parent.iterdir() if p.name.endswith(".tmp")]

    def test_user_install(self, home):
        out = claude_skills.install("deploy-and-test", user=True)
        assert (
            out.path.resolve()
            == (home / ".claude" / "skills" / "deploy-and-test" / "SKILL.md").resolve()
        )

    def test_symlinked_claude_dir_is_followed(self, home, tmp_path):
        """~/.claude kept in a dotfiles repository and symlinked in."""
        dotfiles = tmp_path / "dotfiles" / "claude"
        dotfiles.mkdir(parents=True)
        os.symlink(dotfiles, home / ".claude")
        claude_skills.install("deploy-and-test", user=True)
        assert (dotfiles / "skills" / "deploy-and-test" / "SKILL.md").exists()
        assert claude_skills.status("deploy-and-test", user=True) == "current"

    def test_symlinked_skill_dir_refused(self, proj, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (proj / ".claude" / "skills").mkdir(parents=True)
        os.symlink(elsewhere, proj / ".claude" / "skills" / "deploy-and-test")
        with pytest.raises(claude_skills.SkillError, match="symlink"):
            claude_skills.install("deploy-and-test", project=proj)
        assert list(elsewhere.iterdir()) == []

    def test_missing_project_dir_refused(self, tmp_path):
        missing = tmp_path / "typo" / "proj"
        with pytest.raises(claude_skills.SkillError, match="does not exist"):
            claude_skills.install("deploy-and-test", project=missing)
        assert not (tmp_path / "typo").exists()

    def test_first_install_never_overwrites(self, proj, monkeypatch):
        """A SKILL.md that appears between the check and the write is not
        overwritten without --force."""
        real = claude_skills._read_existing

        def appears(dir_fd, where):
            value = real(dir_fd, where)
            fd = os.open("SKILL.md", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=dir_fd)
            os.write(fd, b"someone else's")
            os.close(fd)
            return value

        monkeypatch.setattr(claude_skills, "_read_existing", appears)
        with pytest.raises(claude_skills.SkillError, match="appeared"):
            claude_skills.install("deploy-and-test", project=proj)
        path = proj / ".claude/skills/deploy-and-test/SKILL.md"
        assert path.read_bytes() == b"someone else's"
        assert [p.name for p in path.parent.iterdir()] == ["SKILL.md"]

    def test_unreadable_status_and_full_path_errors(self, proj):
        out = claude_skills.install("deploy-and-test", project=proj)
        out.path.chmod(0)
        try:
            if os.access(out.path, os.R_OK):
                pytest.skip("running as root")
            assert claude_skills.status("deploy-and-test", project=proj).startswith(
                "unreadable"
            )
        finally:
            out.path.chmod(0o644)
        ro = proj / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            with pytest.raises(claude_skills.SkillError, match=str(ro)):
                claude_skills.install("deploy-and-test", project=ro)
        finally:
            ro.chmod(0o755)

    def test_symlinked_skill_file_not_written_through(self, proj, tmp_path):
        victim = tmp_path / "victim"
        victim.write_text("keep")
        d = proj / ".claude" / "skills" / "deploy-and-test"
        d.mkdir(parents=True)
        os.symlink(victim, d / "SKILL.md")
        with pytest.raises(claude_skills.SkillError, match="not a regular file"):
            claude_skills.install("deploy-and-test", project=proj, force=True)
        assert victim.read_text() == "keep"

    def test_written_with_the_umask(self, proj):
        old = os.umask(0o077)
        try:
            out = claude_skills.install("deploy-and-test", project=proj)
        finally:
            os.umask(old)
        assert oct(out.path.stat().st_mode & 0o777) == oct(0o600)


class TestRound2:
    def test_symlinked_skill_file_named_in_the_error(self, proj, tmp_path):
        d = proj / ".claude" / "skills" / "deploy-and-test"
        d.mkdir(parents=True)
        os.symlink(tmp_path / "x", d / "SKILL.md")
        with pytest.raises(claude_skills.SkillError, match=str(d / "SKILL.md")):
            claude_skills.install("deploy-and-test", project=proj, force=True)

    def test_project_path_that_is_a_file(self, tmp_path):
        f = tmp_path / "file"
        f.write_text("")
        with pytest.raises(claude_skills.SkillError, match="is not a directory"):
            claude_skills.install("deploy-and-test", project=f)

    def test_no_hard_links_falls_back_to_rename(self, proj, monkeypatch):
        def no_links(*a, **k):
            raise OSError(1, "Operation not permitted")  # EPERM, as on vfat

        monkeypatch.setattr(claude_skills.os, "link", no_links)
        out = claude_skills.install("deploy-and-test", project=proj)
        assert out.status == "installed"
        assert out.path.read_bytes() == claude_skills.text("deploy-and-test")
        assert [p.name for p in out.path.parent.iterdir()] == ["SKILL.md"]

    def test_dangling_deploy_config_symlink_refused(self, proj):
        (proj / ".claude").mkdir()
        os.symlink("missing.json", proj / ".claude" / "deploy-config.json")
        with pytest.raises(claude_skills.SkillError, match="not a regular file"):
            claude_skills.write_deploy_config_template(proj)

    def test_cli_and_library_agree_on_the_config_path(self, proj, tmp_path, home):
        dots = tmp_path / "dots"
        dots.mkdir()
        os.symlink(dots, proj / ".claude")
        first = _run("install", "-p", str(proj), "--with-config")
        second = _run("install", "-p", str(proj), "--with-config")
        path = str(dots / "deploy-config.json")
        assert path in first.output and path in second.output

    def test_uninstall_errors_name_the_path(self, proj):
        out = claude_skills.install("deploy-and-test", project=proj)
        out.path.parent.chmod(0o555)
        try:
            if os.access(out.path.parent, os.W_OK):
                pytest.skip("running as root")
            with pytest.raises(claude_skills.SkillError, match=str(out.path)):
                claude_skills.uninstall("deploy-and-test", project=proj)
        finally:
            out.path.parent.chmod(0o755)


class TestUninstall:
    def test_only_labctls_own_skills(self, proj):
        mine = proj / ".claude" / "skills" / "mine"
        mine.mkdir(parents=True)
        (mine / "SKILL.md").write_text("my own skill")
        with pytest.raises(claude_skills.SkillError, match="no bundled skill"):
            claude_skills.uninstall("mine", project=proj, force=True)
        assert (mine / "SKILL.md").read_text() == "my own skill"

    def test_remove_then_absent(self, proj):
        claude_skills.install("deploy-and-test", project=proj)
        out = claude_skills.uninstall("deploy-and-test", project=proj)
        assert out.status == "removed"
        assert not out.path.parent.exists()
        assert claude_skills.uninstall("deploy-and-test", project=proj).status == (
            "absent"
        )

    def test_edited_copy_needs_force(self, proj):
        out = claude_skills.install("deploy-and-test", project=proj)
        out.path.write_text("mine")
        with pytest.raises(claude_skills.SkillError, match="--force"):
            claude_skills.uninstall("deploy-and-test", project=proj)
        assert claude_skills.uninstall(
            "deploy-and-test", project=proj, force=True
        ).status == ("removed")


class TestDeployConfig:
    def test_template_written_once(self, proj):
        path = claude_skills.write_deploy_config_template(proj)
        assert json.loads(path.read_text()) == claude_skills.DEPLOY_CONFIG_EXAMPLE
        path.write_text("{}")
        assert claude_skills.write_deploy_config_template(proj) is None
        assert path.read_text() == "{}"


class TestCli:
    def test_install_list_uninstall(self, proj, home):
        result = _run("install", "-p", str(proj))
        assert result.exit_code == 0, result.output
        assert "Installed" in result.output
        assert "deploy-config.json (not there yet)" in result.output
        assert "MCP server" in result.output
        listed = _run("list", "-p", str(proj)).output
        assert "deploy-and-test: project current; user not installed" in listed
        result = _run("uninstall", "-p", str(proj))
        assert result.exit_code == 0 and "Removed" in result.output

    def test_with_config(self, proj, home):
        result = _run("install", "-p", str(proj), "--with-config")
        assert result.exit_code == 0, result.output
        assert (proj / ".claude" / "deploy-config.json").exists()
        assert "Wrote an example" in result.output
        again = _run("install", "-p", str(proj), "--with-config")
        assert "Using" in again.output

    def test_differs_exits_1(self, proj, home):
        _run("install", "-p", str(proj))
        (proj / ".claude/skills/deploy-and-test/SKILL.md").write_text("x")
        result = _run("install", "-p", str(proj))
        assert result.exit_code == 1
        assert "--force" in result.output

    def test_project_and_user_together_refused(self, proj, home):
        result = _run("install", "-p", str(proj), "--user")
        assert result.exit_code == 2

    def test_user_with_config_refused(self, home):
        result = _run("install", "--user", "--with-config")
        assert result.exit_code == 2
        assert not (home / ".claude" / "skills").exists()

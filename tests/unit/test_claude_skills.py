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

    def test_symlinked_skills_dir_refused(self, proj, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (proj / ".claude").mkdir()
        os.symlink(elsewhere, proj / ".claude" / "skills")
        with pytest.raises(claude_skills.SkillError, match="symlink"):
            claude_skills.install("deploy-and-test", project=proj)
        assert list(elsewhere.iterdir()) == []

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


class TestUninstall:
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

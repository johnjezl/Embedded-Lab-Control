"""The Python snippets embedded in scripts/update.sh must keep working
against the labctl API they import (they run on production deploys)."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

UPDATE_SH = Path(__file__).resolve().parents[2] / "scripts" / "update.sh"


def _snippet(marker: str) -> str:
    """The heredoc body that follows the line containing ``marker``."""
    text = UPDATE_SH.read_text()
    match = re.search(
        re.escape(marker) + r"[^\n]*<<'PY'[^\n]*\n(.*?)\nPY\n", text, flags=re.S
    )
    assert match, f"no PY heredoc after {marker!r} in update.sh"
    return match.group(1)


def _run(snippet: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-", *args],
        input=snippet,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def write_config(tmp_path):
    def write(body: str) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(f"database_path: {tmp_path / 'x.db'}\n{body}")
        return path

    return write


class TestMcpAuthCheck:
    """Step 3c: report what labctl-mcp will require or refuse."""

    SNIPPET_MARKER = 'MCP_AUTH=$("$LABCTL_VENV/bin/python"'

    def check(self, config: Path, host: str = "127.0.0.1") -> str:
        result = _run(_snippet(self.SNIPPET_MARKER), str(config), host)
        assert result.returncode == 0, result.stderr
        return result.stdout.strip()

    def test_no_auth_on_loopback(self, write_config):
        assert self.check(write_config("")) == "none"

    def test_auth_required(self, write_config):
        config = write_config(
            "auth:\n  enabled: true\n  users:\n"
            "    - username: a\n      api_key: kkkkkkkkkkkkkkkkkkkkkkkk\n"
        )
        assert self.check(config) == "required"

    def test_auth_without_keys_refused(self, write_config):
        config = write_config("auth:\n  enabled: true\n  users:\n    - username: a\n")
        assert self.check(config).startswith("refused: auth.enabled is true")

    def test_non_loopback_without_auth_refused(self, write_config):
        out = self.check(write_config(""), host="0.0.0.0")
        assert out.startswith("refused: Refusing to serve MCP over HTTP on 0.0.0.0")


class TestAllowlistCheck:
    """Step 3b: exit status says whether an allowlist key is set."""

    SNIPPET_MARKER = 'if ! "$LABCTL_VENV/bin/python" - "$cfg" "$key"'

    def test_set_and_missing(self, write_config):
        config = write_config("mcp:\n  allowed_read_paths: [/var/lib/labctl/images]\n")
        snippet = _snippet(self.SNIPPET_MARKER)
        assert _run(snippet, str(config), "allowed_read_paths").returncode == 0
        assert _run(snippet, str(config), "allowed_write_paths").returncode == 1

"""A real stdio MCP server process and its session claim (#73): when the
agent goes away the board is released.

The server is driven over its stdin/stdout with JSON-RPC, as an MCP
client would: initialize, then claim_sbc(hold="session").
"""

import json
import os
import signal
import subprocess
import sys

import pytest

from labctl.core.database import get_database
from labctl.core.manager import ResourceManager

pytest.importorskip("mcp")


@pytest.fixture
def lab(tmp_path):
    db = tmp_path / "lab.db"
    manager = ResourceManager(get_database(db))
    manager.create_sbc(name="pi-1")
    config = tmp_path / "config.yaml"
    config.write_text(f"database_path: {db}\n")
    return manager, config


class StdioServer:
    def __init__(self, config):
        self.proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from labctl.cli import main; main()",
                "-c",
                str(config),
                "mcp",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        self._id = 0

    def request(self, method, params):
        self._id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        )
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise AssertionError(
                    f"server exited: {self.proc.stderr.read()[-2000:]}"
                )
            message = json.loads(line)
            if message.get("id") == self._id:
                return message

    def _send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def start(self):
        reply = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert reply["result"]["serverInfo"]["name"] == "labctl"
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self

    def call(self, tool, **arguments):
        reply = self.request("tools/call", {"name": tool, "arguments": arguments})
        return reply["result"]["content"][0]["text"]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:
                pass


@pytest.fixture
def server(lab):
    srv = StdioServer(lab[1]).start()
    yield srv
    srv.stop()


def _claim_session(server, manager):
    result = json.loads(
        server.call("claim_sbc", sbc_name="pi-1", hold="session", agent_name="t")
    )
    assert result["status"] == "claimed", result
    claim = manager.get_active_claim("pi-1")
    assert claim.session_held and claim.session_id.startswith("mcp-stdio:")
    return claim


def test_agent_gone_releases_the_board(lab, server):
    """The agent exits (or is killed): its end of the pipe closes, the
    server ends the session and releases the claim."""
    manager, _ = lab
    _claim_session(server, manager)
    server.proc.stdin.close()
    assert server.proc.wait(timeout=30) == 0
    assert manager.get_open_claim("pi-1") is None
    [released] = manager.list_claim_history("pi-1")
    assert released.release_reason.value == "released"


def test_killed_server_is_released_by_the_next_sweep(lab, server):
    """No clean exit at all: any process's dead-session sweep (another MCP
    server, `labctl claims expire`) releases a stdio session claim at
    once instead of waiting out its heartbeat window."""
    manager, _ = lab
    _claim_session(server, manager)
    server.proc.send_signal(signal.SIGKILL)
    server.proc.wait(timeout=10)
    assert manager.get_active_claim("pi-1") is not None  # not swept yet
    assert manager.release_dead_sessions(grace_seconds=60) == 1
    assert manager.get_open_claim("pi-1") is None
    [released] = manager.list_claim_history("pi-1")
    assert released.release_reason.value == "session-lost"

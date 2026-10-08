"""docs/OPERATIONS.md must list every MCP tool, resource and CLI command.

The classification (issue #7, D012) is documentation; this keeps it honest:
adding a tool or command without classifying it fails here, and the MCP
"Confirm" column must match the tools that actually require confirmation.
"""

import re
from pathlib import Path

import anyio
import click

DOC = Path(__file__).resolve().parents[2] / "docs" / "OPERATIONS.md"
CLASSES = {
    "read",
    "db-write",
    "shared-resource",
    "system-write",
    "hardware",
    "destructive",
    "server",
}


def _section_rows(heading: str) -> dict[str, list[str]]:
    """Rows of the first table after ``## heading``: {name: other cells}."""
    text = DOC.read_text()
    start = text.index(f"## {heading}\n")
    end = text.find("\n## ", start + 1)
    rows = {}
    for line in text[start : end if end != -1 else None].splitlines():
        m = re.match(r"^\| `([^`]+)` \|(.*)\|$", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in m.group(2).split("|")]
    return rows


def test_every_mcp_tool_classified_and_confirm_column_matches():
    from labctl.mcp_server import _DESTRUCTIVE_TOOLS, mcp

    rows = _section_rows("MCP tools")
    registered = {t.name for t in anyio.run(mcp.list_tools)}
    assert set(rows) == registered
    for name, (cls, confirm, _notes) in rows.items():
        assert cls in CLASSES, name
        assert (confirm == "yes") == (name in _DESTRUCTIVE_TOOLS), name


def test_every_mcp_resource_classified():
    from labctl.mcp_server import mcp

    rows = _section_rows("MCP resources")
    registered = {str(r.uri) for r in anyio.run(mcp.list_resources)}
    registered |= {r.uri_template for r in anyio.run(mcp.list_resource_templates)}
    assert set(rows) == registered


def _cli_commands(group, prefix=""):
    for name, cmd in group.commands.items():
        if cmd.hidden:
            continue
        full = f"{prefix} {name}".strip()
        if isinstance(cmd, click.Group):
            yield from _cli_commands(cmd, full)
        else:
            yield full, cmd


def test_every_cli_command_classified_and_prompts_column_matches():
    from labctl.cli import main

    rows = _section_rows("CLI commands")
    commands = dict(_cli_commands(main))
    assert set(rows) == set(commands)
    for name, (cls, prompts, _notes) in rows.items():
        assert cls in CLASSES, name
        has_yes = any(
            "--yes" in p.opts
            for p in commands[name].params
            if isinstance(p, click.Option)
        )
        assert (prompts == "yes") == has_yes, name

# Security Policy

## Supported versions

labctl (`embedded-lab-control` on PyPI) is pre-1.0. Security fixes go into
the latest release only; upgrade to get them.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it
privately through GitHub: on the repository's **Security** tab, choose
**Report a vulnerability**.

Include what you can of: the affected version, the configuration involved
(transport, `--host`, `auth.enabled`, relevant `mcp.*` settings), steps to
reproduce, and the impact you expect. You should get an acknowledgement
within a week; fixes are released as soon as they're ready, and reporters
are credited unless they ask not to be.

## Scope

labctl controls real hardware: power, SD cards, serial consoles and
actuators. Of particular interest:

- Ways around the MCP server's controls: HTTP authentication, Host/Origin
  checks, destructive-tool confirmation, the host file allowlist, claims,
  or the privileged actuator gate.
- Reading or writing host files outside the configured allowlist.
- Web UI / REST API authentication or CSRF bypasses.
- Leaks of API keys, passwords or Kasa credentials through logs, the audit
  trail or API responses.

Out of scope: anything that needs a user who is already trusted with the
CLI (it runs with the invoking user's own permissions), or an
unauthenticated MCP server deliberately exposed contrary to the
documentation.

How the controls fit together, and their known limits, is described in
[`docs/MCP_SERVER.md`](docs/MCP_SERVER.md#security-considerations).

# Security Policy

## Supported versions

labctl is pre-1.0 and not yet published on PyPI (it will be, as
`embedded-lab-control`); install it from this repository. Security fixes
go into the latest version only; upgrade to get them.

## Reporting a vulnerability

Please **don't open a public issue** for a security problem. Report it
privately through GitHub: on the repository's **Security** tab, choose
**Report a vulnerability**.

Include what you can of: the affected version, the configuration involved
(transport, `--host`, `auth.enabled`, relevant `mcp.*` settings), steps to
reproduce, and the impact you expect. You should get an acknowledgement
within a week; fixes are released as soon as they're ready, and reporters
are credited unless they ask not to be.

## Controls at a glance

- **MCP over stdio:** only the local user who starts `labctl mcp`.
- **MCP over HTTP:** bound to `127.0.0.1` by default. It refuses to start
  without authentication on a non-loopback address, or when
  `mcp.allowed_hosts`/`allowed_origins` accept non-loopback names. With
  `auth.enabled`, clients send `Authorization: Bearer <api_key>`.
  `Host`/`Origin` headers are checked (DNS rebinding). TLS is available
  with `--cert`/`--key` or `mcp.cert_file`/`key_file`, and the server
  warns when keys would cross the network unencrypted.
- **Web UI / REST API:** login sessions and `X-API-Key`. These are only
  enforced with `auth.enabled`, and so is CSRF protection on web forms.
  TLS via `web.cert_file`/`key_file` or `--cert`/`--key`. See "Web UI
  exposure" below.
- **Roles** (`auth.users[].role`): `operator` (default) or `agent`. Over
  MCP, REST and the web UI, only operators may force-release another
  user's claim, or override it (REST/web only). Agents can only ask
  (`request_sbc_release`). The CLI acts as an operator (`--override-claim`).
- **Claims:** a claimed board, and hardware it shares with other boards,
  can't be operated or reconfigured by other users on MCP, REST, the web
  UI or the CLI. Overrides are recorded in the activity log.
  `claims.require_claim` makes MCP and REST callers claim first.
- **Destructive MCP tools** act only on a second call carrying a
  single-use `confirm_token` (`mcp.confirm_destructive`, on by default).
- **Host file allowlist:** MCP tools may read or write host files only
  under `mcp.allowed_read_paths`/`allowed_write_paths` (empty by default,
  i.e. nothing). Paths are resolved first and opened without following
  symlinks.
- **Image uploads** (`PUT /images/<name>` on the MCP HTTP port): off
  unless `mcp.upload_dir` is set. They take bare names only, have size
  and free-space limits, and only the uploader or an operator may
  replace or delete an image.
- **Raw actuator tools** (`actuator_add/remove/set` over MCP): off unless
  `mcp.allow_admin_actuator_ops: true`.
- **Fail closed on config:** `labctl web` and `labctl mcp --http` refuse to
  start if a config file they should read can't be loaded.
- **Audit:** changes are recorded with the acting user/session
  (`labctl activity tail`).
- **CLI:** runs with the invoking user's permissions; it isn't limited by
  the `mcp.*` settings.

### Web UI exposure

The shipped `config/systemd/labctl-web.service` runs `labctl web --host
0.0.0.0` over plain HTTP, and `auth.enabled` is off by default. With that
combination, anyone who can reach port 5000 can use the web UI and REST
API, including power control. Without auth there is also no CSRF check,
so a web page opened by someone on the lab network can submit the web
UI's forms. Unlike the MCP server, `labctl web` does **not** refuse an
unauthenticated bind beyond loopback. Before exposing it, enable
`auth.enabled` (and TLS, `web.cert_file`/`key_file`), or change the unit
to `--host 127.0.0.1` and reach it through an SSH tunnel or a
TLS-terminating proxy.

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
CLI (it runs with the invoking user's own permissions); an
unauthenticated MCP server exposed beyond the local host despite its
startup refusal (e.g. behind a reverse proxy that forwards
`Host: 127.0.0.1`); and an unauthenticated web UI / REST API reachable
from the network (see "Web UI exposure": that is how it behaves without
auth, not a bypass). With auth enabled, a way around authentication or
CSRF protection is in scope.

How the controls fit together, and their known limits, is described in
[`docs/MCP_SERVER.md`](docs/MCP_SERVER.md#security-considerations).

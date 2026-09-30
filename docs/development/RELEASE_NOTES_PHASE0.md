# Phase 0 scratch list — noticed, not fixed

Working notes from the Phase 0 (installable + mcp 2.x SDK) pass. Items here are
out of Phase 0 scope and are tagged with the phase they most likely belong to.
Delete or fold into the real release notes before publishing.

## Deployment impact of Phase 0 (read before deploying to tarrasque)

- **MCP HTTP port moves 8000 → 8080.** Production `labctl-mcp` runs
  `mcp --http 8080` but, because `run_server` ignored `http_port`, it has
  actually been listening on `127.0.0.1:8000` (confirmed with `ss -ltn` on
  2026-09-29). After deploying Phase 0 it will listen on `127.0.0.1:8080`, as
  the unit and docs always claimed. Any client wired to `:8000` must move.
  No local `.mcp.json` / `~/.claude.json` entry referencing either port was found.
- **The documented remote URL `http://tarrasque:8080/mcp` does not work** with
  the loopback default: the port is not reachable remotely, and even behind a
  proxy the SDK's DNS-rebinding guard returns 421 for a non-localhost `Host`.
  Remote access needs a tunnel or `--host` — which ties into auth (P1).
- **Production venv is on mcp 1.26.0** under the `PYTHONPATH` overlay (#11).
  Upgrading to mcp 2.x changes the dependency tree the overlay was built for
  (`httpx` → `httpx2`, new `mcp-types`, `opentelemetry-api`); rebuild the
  overlay or, better, fix the disk first.
- **Tool calls remain serialized** (process-wide lock, D011), matching 1.x.

## P1 safety

- `--host 0.0.0.0` on the HTTP transport exposes all 52 tools with no auth.
  The flag's help text warns; real auth is P1.
- `sdwire_ls` / `sdwire_cat` / `sdwire_info` are annotated read-only but
  physically flip the SD mux to host and back, and require a *mutating* claim.
  Annotation is accurate (no lasting change), but a user reading
  "read-only" may not expect the mux to move.
- `actuator_probe` is annotated read-only but writes `last_probe_*` columns.
- `serial_capture` is read-only but opens a ser2net session that may interleave
  with another user's session (issue #7).
- Tool annotations are hints for clients; nothing server-side enforces
  read-only vs destructive. Confirm/dry-run for destructive tools is P1.
- Web UI falls back to a fixed `SECRET_KEY = "labctl-dev-key"` when auth is
  disabled and no key is configured (`web/app.py`). Harmless while auth is
  off, but a public release should not ship a constant session secret.

## P2 release

- ~~No LICENSE file~~ — resolved by `11ad19f` on main (MIT, © 2026 John
  Jezl). Verified: wheel ships `dist-info/licenses/LICENSE` with
  `License-Expression: MIT` / `License-File: LICENSE`; sdist includes it.
- sdist contains only `src/`, `README.md`, `pyproject.toml`: no tests,
  `CHANGELOG.md`, `config/` (systemd units, sample config) or `docs/`. Decide
  what a PyPI sdist should carry (`MANIFEST.in`).
- Only `labctl mcp` gives a friendly "install the extra" error on a bare
  install. Other extra-gated commands (`web` → flask, `kasa`, `sdwire`,
  actuators → pyserial) likely still surface raw `ModuleNotFoundError`s or
  RuntimeErrors naming `pip install sdwire` rather than the extra.
- sdwire error text says `pip install sdwire`; on Python < 3.12 that cannot
  succeed. Consider mentioning the 3.12 requirement.
- ~~Repo-wide lint debt~~ — resolved in `120990e` (black/isort/flake8 clean,
  tools pinned exactly in the `dev` extra).
- CI notes (from writing `.github/workflows/ci.yml`):
  - Test coverage is **68%** (`cli.py` 51%, `mcp_server.py` 78%); CI reports
    it but does not gate on it. AGENT_RULES aims for > 80%.
  - Push + pull_request triggers mean a same-repo PR branch runs twice.
    Consider `push: branches: [main]` once the PR flow is settled.
  - Python 3.11 and 3.13 have only been exercised by CI, not locally.
  - Runners have passwordless sudo; the suite passed locally in a sandbox
    that hid `/etc/labctl`, `~/.config/labctl` and `ser2net`, but any test
    that shells out to `sudo` unmocked would really run it on a runner.
  - gitleaks: 4 historical false positives (README `your-api-key`
    placeholders, test fixture `test-api-key-abc123`) are allowlisted by
    exact fingerprint in `.gitleaksignore`.
  - Consider a coverage service and a PyPI publish workflow (trusted
    publishing, needs an `id-token: write` job) in P2.
- `mcp 2.x` pulls `mcp-types==<exact>`, `httpx2`, `opentelemetry-api`; worth a
  line in the release notes for downstream packagers.

## Test-suite health

- **Suite ran 32 min locally on tarrasque**: `/tmp` is on the failing
  `/dev/sda` (#11) and each SQLite commit takes ~1.2 s there vs ~6 ms on tmpfs.
  With `--basetemp` on `/dev/shm` the full suite takes ~2 min. Not a code
  problem, but anyone running tests on tarrasque is also stressing the bad disk.
- `test_manager.py::TestClaimExpiryAndHeartbeat::test_heartbeat_prevents_expiry`
  uses real sleeps and failed once under load (passes in isolation) — CI flake
  risk.
- Several tests take 3–5 s on real `time.sleep` / timeouts
  (`test_web::test_power_action_unknown_action`,
  `test_mcp_server::test_sdwire_to_host_with_device`,
  `test_enter_recovery_blocked_without_claim`, recovery-sequence tests).
  Worth checking whether any attempt real network I/O to fixture IPs
  (e.g. `192.168.1.200`).

## P3 docs

- `docs/MCP_SERVER.md` remote-client example (`http://tarrasque:8080/mcp`)
  should be rewritten around whatever P1 decides for remote access.
- README lists `labctl mcp --http 8080` without `--host`.

## Fixed during Phase 0 (for the real release notes)

- **Python 3.10 was broken for SDWire**: `labctl/sdwire/controller.py` used
  `datetime.UTC` (3.11+), so importing `labctl.sdwire` crashed on 3.10 despite
  `requires-python >= 3.10`. Now `timezone.utc`. All other modules verified to
  import on 3.10.
- `test_sdwire.py::test_discover_import_error` tested a local copy of the
  import logic, not the real function; now exercises the real code.
- Web `/api/health` and the page footer hard-coded `0.1.0`; both now read
  `labctl.__version__` (single source for the package version too).

## Tooling / environment

- `gh issue view N` (non-JSON) prints nothing and exits 0 in this
  environment; `gh issue view N --json ...` works.
- The shared repo `.venv` imports `labctl` from the main checkout, not from
  worktrees, and still has mcp 1.26 — it cannot validate worktree changes.

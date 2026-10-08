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
- ~~Production venv on the `PYTHONPATH` overlay~~ — resolved 2026-10-05: disk
  fixed, overlay drop-in and `/opt/labctl/overlay` removed, #11 closed.
  Production is still on mcp 1.26 / labctl 0.1.0 until Phase 0 is deployed.
- **The first Phase 0 deploy migrates the distribution.** Production has the
  old `labctl` 0.1.0 distribution; `update.sh` now uninstalls it before
  installing `embedded-lab-control` (otherwise both would own `labctl/`, and a
  later `pip uninstall labctl` would delete the new install). Verified on a
  scratch venv seeded from `11ad19f`: migration run and a same-version rerun
  both leave exactly one distribution, `pip check` clean.
- **Tool calls and hardware-touching resources are serialized** (process-wide
  lock, D011), matching 1.x for everything that touches hardware.
- `scripts/update.sh` should surface per-service start failures more loudly
  (carried over from #11: a failed `labctl-mcp` only showed as
  `[!!] labctl-mcp FAILED` at the end). P2.

## P1 safety

- `--host 0.0.0.0` on the HTTP transport exposes all 52 tools with no auth.
  The flag's help text warns; real auth is P1.
- ~~`sdwire_ls` / `sdwire_cat` / `sdwire_info` moved the mux and failed open~~
  — resolved in Phase 1 WS1: read in place when already on host, switch only
  when the board is known OFF, otherwise refuse; annotated read-only again.
- `actuator_probe` is annotated read-only but writes `last_probe_*` columns.
- `serial_capture` is read-only but opens a ser2net session that may interleave
  with another user's session (issue #7).
- Tool annotations are hints for clients; nothing server-side enforces
  read-only vs destructive. Confirm/dry-run for destructive tools is P1.
- ~~Web UI constant `SECRET_KEY = "labctl-dev-key"`~~ — resolved in Phase 1
  WS1 (random per process). Correction to the earlier note: it was not
  harmless with auth off, because the signed session carries the CSRF token.

## P2 release

- **sudoers gap:** the controller runs `sudo blkid` (`get_disk_info`, used
  by `sdwire_info`, and partition listing), but neither the README's
  sudoers line nor tarrasque's `/etc/sudoers.d/labctl` allows `blkid`, and
  the README also lists `partprobe`, which tarrasque's file lacks. Under the
  service (no TTY) those calls fail. The install script doesn't write this
  rule at all. Generate it from one list in `install-services.sh` (P2).

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
- ~~Repo-wide lint debt~~ — resolved in `ea3a13a` (black/isort/flake8 clean,
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
- First CI run: 995/996 on every Python version; the one failure was
  `test_services.py::test_parse_systemd_timestamp_valid`, which parsed a
  hard-coded "PDT" timestamp and so only passed on a Pacific-time host.
  Fixed by pinning TZ in the test. Product note: `_parse_systemd_timestamp`
  returns None for any zone abbreviation other than UTC/GMT/the host's own
  (strptime `%Z` limitation) — correct for local systemd output, but it
  would silently drop timestamps if ever fed output from another host.
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

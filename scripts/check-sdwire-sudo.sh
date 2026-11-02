#!/bin/bash
# Check that each user can run the SDWire commands through sudo without a
# password, and print the rule to add if not (#47).
#
# Usage: sudo ./scripts/check-sdwire-sudo.sh USER [USER...]
#
# labctl's SDWire code runs these as root via sudo, with no terminal to ask
# for a password (the MCP service, agents' stdio MCP servers, scripts):
# flashing, file updates and reading SD cards fail without them. The rule
# is not installed automatically: passwordless root dd and mount amount to
# root for whoever holds it, so granting it is the admin's decision (#47,
# option (a)). This script only reports and always exits 0, so the install
# and update scripts can call it as a warning.
#
# Rather than interpret `sudo -l` output (tags, runas, negations, defaults,
# argument patterns, locale), it asks sudo itself: it becomes each user
# with runuser (no sudoers entry for root needed) and runs
# `sudo -n -k <command> --version`, which sudo allows only if no password
# is needed (-k: a cached login doesn't count), and which changes nothing.
# Limits: a rule that only allows certain arguments (e.g. `dd of=/dev/sd*`)
# doesn't allow `--version`, so it shows as missing; and sudo logs each
# refused probe (and may mail root about it), as it would any refusal.

set -u

# The commands labctl runs as `sudo <cmd>` (tests/unit/test_check_sdwire_
# sudo.py checks this against the code).
COMMANDS="mount umount dd sync partprobe parted blkid"
RULE_FILE="/etc/sudoers.d/labctl-sdwire"

# A rule line for USER covering every command at both of its usual paths.
rule_for() {
    local user="$1" paths="" cmd dir
    for cmd in $COMMANDS; do
        case "$cmd" in
            partprobe|parted|blkid) dir=sbin ;;
            *) dir=bin ;;
        esac
        paths="$paths${paths:+, }/usr/$dir/$cmd, /$dir/$cmd"
    done
    echo "$user ALL=(root) NOPASSWD: $paths"
}

# The installed commands USER can't run as root through sudo without a
# password.
missing_for() {
    local user="$1" cmd absent=()
    for cmd in $INSTALLED; do
        # As USER, with no terminal: sudo -n fails instead of asking.
        if ! runuser -u "$user" -- sudo -n -k "$cmd" --version \
            </dev/null >/dev/null 2>&1; then
            absent+=("$cmd")
        fi
    done
    echo "${absent[*]:-}"
}

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 USER [USER...]" >&2
    exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "[!] check-sdwire-sudo.sh must run as root (to act as other users); skipped"
    exit 0
fi

if ! command -v runuser >/dev/null 2>&1; then
    echo "[!] check-sdwire-sudo.sh needs runuser (util-linux); skipped"
    exit 0
fi

# A command that isn't installed is reported as such, not as a missing
# rule (a rule wouldn't help).
INSTALLED="" NOT_INSTALLED=""
for cmd in $COMMANDS; do
    if command -v "$cmd" >/dev/null 2>&1; then
        INSTALLED="$INSTALLED${INSTALLED:+ }$cmd"
    else
        NOT_INSTALLED="$NOT_INSTALLED${NOT_INSTALLED:+ }$cmd"
    fi
done
if [ -n "$NOT_INSTALLED" ]; then
    echo "[!] SDWire commands not installed: $NOT_INSTALLED (SD card operations that use them fail)"
fi

problems=0
for user in "$@"; do
    if [ -z "$user" ]; then
        # The install/update scripts pass ${SUDO_USER:-}: empty when run
        # as root directly, so the admin's own account isn't known.
        echo "[i] not run through sudo, so only the users named were checked; to check yours:"
        echo "    sudo $0 YOUR_USER"
        continue
    fi
    if [ "$user" = "root" ]; then
        continue
    fi
    if ! id "$user" >/dev/null 2>&1; then
        echo "[!] no user $user; skipped"
        continue
    fi
    gaps=$(missing_for "$user")
    if [ -z "$gaps" ]; then
        echo "[ok] $user can run the SDWire commands through sudo"
        continue
    fi
    problems=1
    echo "[!] $user can't run these SDWire commands through sudo without a password: $gaps"
    echo "    SD card flashing, updates and reads (MCP, CLI) fail for $user until a rule allows them."
    echo "    That grants root dd and mount to $user (effectively root); if you accept that, run"
    echo "    (it keeps the rules already in $RULE_FILE and checks the result before installing it):"
    echo "      sudo sh -c 't=\$(mktemp) && { [ ! -e $RULE_FILE ] || cat $RULE_FILE; } > \"\$t\" && echo \"$(rule_for "$user")\" >> \"\$t\" && visudo -cf \"\$t\" && install -m 440 -o root -g root \"\$t\" $RULE_FILE; s=\$?; rm -f \"\$t\"; exit \$s'"
    echo "    (If your rule allows only certain arguments, it can't be seen by this check; ignore this.)"
done
if [ "$problems" -ne 0 ]; then
    echo "    See README.md, \"SDWire (SD Card Multiplexer)\", and issue #47."
fi
exit 0

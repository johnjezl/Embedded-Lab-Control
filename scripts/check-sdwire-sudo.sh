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
# It checks sudoers only: a systemd unit's sandboxing is not applied.

set -u

# The commands labctl runs as `sudo <cmd>` (tests/unit/test_check_sdwire_
# sudo.py checks this against the code).
COMMANDS="mount umount dd sync partprobe parted blkid"
RULE_FILE="/etc/sudoers.d/labctl-sdwire"

# A rule line for USER covering every command at the path found (the one
# sudo runs) and, for /usr/bin and /usr/sbin, the same path without /usr.
rule_for() {
    local user="$1" paths="" path
    for path in $INSTALLED_PATHS; do
        paths="$paths${paths:+, }$path"
        case "$path" in
            /usr/bin/* | /usr/sbin/*) paths="$paths, ${path#/usr}" ;;
        esac
    done
    echo "$user ALL=(root) NOPASSWD: $paths"
}

# The installed commands USER can't run as root through sudo without a
# password. A probe that takes over 10 s (e.g. sudo rules from an
# unreachable directory) is reported apart, as "timed out: CMD".
missing_for() {
    local user="$1" cmd status absent=""
    for cmd in $INSTALLED; do
        # As USER, with no terminal: sudo -n fails instead of asking.
        timeout 10 runuser -u "$user" -- sudo -n -k "$cmd" --version \
            </dev/null >/dev/null 2>&1
        status=$?
        if [ "$status" -eq 124 ]; then
            absent="$absent${absent:+, }timed out: $cmd"
        elif [ "$status" -ne 0 ]; then
            absent="$absent${absent:+, }$cmd"
        fi
    done
    echo "$absent"
}

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 USER [USER...]" >&2
    exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "[!] check-sdwire-sudo.sh must run as root (to act as other users); skipped"
    exit 0
fi

for tool in runuser timeout; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "[!] check-sdwire-sudo.sh needs $tool; skipped"
        exit 0
    fi
done
if ! command -v sudo >/dev/null 2>&1; then
    echo "[!] sudo is not installed: labctl's SD card operations (flashing, updates, reads) need it"
    exit 0
fi

# A command that isn't installed is reported as such, not as a missing
# rule (a rule wouldn't help).
INSTALLED="" INSTALLED_PATHS="" NOT_INSTALLED=""
for cmd in $COMMANDS; do
    if path=$(command -v "$cmd"); then
        INSTALLED="$INSTALLED${INSTALLED:+ }$cmd"
        INSTALLED_PATHS="$INSTALLED_PATHS${INSTALLED_PATHS:+ }$path"
    else
        NOT_INSTALLED="$NOT_INSTALLED${NOT_INSTALLED:+ }$cmd"
    fi
done
if [ -n "$NOT_INSTALLED" ]; then
    echo "[!] SDWire commands not installed: $NOT_INSTALLED (SD card operations that use them fail)"
fi

problems=0
seen=" "
for user in "$@"; do
    if [ -z "$user" ] || [ "$user" = "root" ]; then
        # The install/update scripts pass ${SUDO_USER:-}: empty (or root)
        # when run from a root shell, so the admin's own account isn't
        # known. root itself needs no rule.
        echo "[i] run from a root shell, so only the other users named were checked; to check yours:"
        echo "    sudo $0 YOUR_USER"
        continue
    fi
    case "$seen" in
        *" $user "*) continue ;;
    esac
    seen="$seen$user "
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

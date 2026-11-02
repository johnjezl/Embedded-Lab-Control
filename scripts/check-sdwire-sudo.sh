#!/bin/bash
# Check that each user can run the SDWire commands through sudo without a
# password, and print the rule to add if not (#47).
#
# Usage: sudo ./scripts/check-sdwire-sudo.sh USER [USER...]
#
# labctl's SDWire code runs these as root via sudo, with no terminal to ask
# for a password (the web and MCP services, and agents' stdio MCP servers):
# flashing, file updates and reading SD cards fail without them. The rule
# is not installed automatically: passwordless root dd and mount amount to
# root for whoever holds it, so granting it is the admin's decision (#47,
# option (a)). This script only reports; it changes nothing and always
# exits 0, so the install and update scripts can call it as a warning.

set -u

# The commands labctl runs as `sudo <cmd>`, and the directories sudo may
# resolve each to (merged /usr or not).
COMMANDS="mount umount dd sync partprobe parted blkid"
RULE_FILE="/etc/sudoers.d/labctl-sdwire"

# A rule line for USER covering every command at both of its usual paths.
rule_for() {
    local user="$1" cmds=() cmd dir
    for cmd in $COMMANDS; do
        case "$cmd" in
            partprobe|parted|blkid) dir=sbin ;;
            *) dir=bin ;;
        esac
        cmds+=("/usr/$dir/$cmd" "/$dir/$cmd")
    done
    local IFS=","
    echo "$user ALL=(root) NOPASSWD: ${cmds[*]}" | sed 's/,/, /g'
}

# The commands USER may NOT run without a password, from `sudo -l -U`.
missing_for() {
    local user="$1" listing nopasswd cmd
    if ! listing=$(sudo -n -l -U "$user" 2>/dev/null); then
        echo "$COMMANDS"  # unknown user or no rules at all
        return
    fi
    # Only entries tagged NOPASSWD count: nothing can type a password.
    nopasswd=$(printf '%s\n' "$listing" | grep 'NOPASSWD:' || true)
    if printf '%s\n' "$nopasswd" | grep -qE 'NOPASSWD:[[:space:]]*ALL([[:space:]]|$)'; then
        return  # everything allowed
    fi
    local absent=()
    for cmd in $COMMANDS; do
        if ! printf '%s\n' "$nopasswd" \
            | grep -qE "(^|[[:space:],:])/(usr/)?s?bin/$cmd([[:space:],]|$)"; then
            absent+=("$cmd")
        fi
    done
    echo "${absent[*]:-}"
}

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 USER [USER...]" >&2
    exit 0
fi

problems=0
for user in "$@"; do
    if [ -z "$user" ] || [ "$user" = "root" ]; then
        continue
    fi
    gaps=$(missing_for "$user")
    if [ -z "$gaps" ]; then
        echo "[ok] $user can run the SDWire commands through sudo"
        continue
    fi
    problems=1
    echo "[!] $user can't run these SDWire commands through sudo without a password: $gaps"
    echo "    SD card flashing, updates and reads (web, MCP, CLI) fail for $user until a rule allows them."
    echo "    That grants root dd and mount to $user (effectively root); if you accept that, run:"
    echo "      echo '$(rule_for "$user")' | sudo tee -a $RULE_FILE"
    echo "      sudo chmod 440 $RULE_FILE && sudo visudo -c"
done
if [ "$problems" -ne 0 ]; then
    echo "    See README.md, \"SDWire (SD Card Multiplexer)\", and issue #47."
fi
exit 0

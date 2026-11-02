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
# option (a)). This script only reports; it changes nothing and always
# exits 0, so the install and update scripts can call it as a warning.

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

# From a `sudo -l -U` listing, one line per rule item that applies as
# root, in order: "allow PATH" (passwordless, any arguments), "deny PATH"
# (a "!" negation), or "allow ALL" / "deny ALL". Entries wrapped onto
# indented continuation lines are joined; a tag (NOPASSWD:, PASSWD:)
# applies until the next one, as sudo prints them; "Defaults !authenticate"
# makes untagged items passwordless. A command restricted to arguments
# doesn't count: labctl passes its own.
parse_rules() {
    awk '
    /^Matching Defaults entries/ { section = "defaults"; next }
    /may run the following commands/ { section = "rules"; next }
    section == "defaults" && /!authenticate/ { noauth = 1 }
    section != "rules" { next }
    /^[[:space:]]+\(/ { if (entry != "") entries[++n] = entry; entry = $0; next }
    /^[[:space:]]+[^[:space:](]/ { entry = entry " " $0; next }
    END {
        if (entry != "") entries[++n] = entry
        for (i = 1; i <= n; i++) {
            e = entries[i]
            sub(/^[[:space:]]*\(/, "", e)
            runas = substr(e, 1, index(e, ")") - 1)
            e = substr(e, index(e, ")") + 1)
            split(runas, parts, ":")
            if (parts[1] !~ /(^|[ ,])(root|ALL)([ ,]|$)/) continue
            pw = noauth ? "nopasswd" : "passwd"
            count = split(e, items, ",")
            for (j = 1; j <= count; j++) {
                item = items[j]
                while (match(item, /^[[:space:]]*[A-Z_]+:/)) {
                    tag = substr(item, RSTART, RLENGTH)
                    gsub(/[[:space:]:]/, "", tag)
                    if (tag == "NOPASSWD") pw = "nopasswd"
                    if (tag == "PASSWD") pw = noauth ? "nopasswd" : "passwd"
                    item = substr(item, RSTART + RLENGTH)
                }
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", item)
                if (item == "") continue
                if (substr(item, 1, 1) == "!") {
                    path = substr(item, 2); sub(/[[:space:]].*/, "", path)
                    print "deny " path
                    continue
                }
                path = item; sub(/[[:space:]].*/, "", path)
                args = item; sub(/^[^[:space:]]*[[:space:]]*/, "", args)
                if (pw == "nopasswd" && (args == "" || args == "*"))
                    print "allow " path
            }
        }
    }'
}

# The commands USER may NOT run as root without a password.
missing_for() {
    local user="$1" listing rules cmd verdict verb path
    if ! listing=$(sudo -n -l -U "$user" 2>/dev/null); then
        echo "$COMMANDS"  # unknown user or no rules at all
        return
    fi
    rules=$(printf '%s\n' "$listing" | parse_rules)
    local absent=()
    for cmd in $COMMANDS; do
        verdict=deny  # sudo: the last matching item wins
        while read -r verb path; do
            [ -n "$verb" ] || continue
            case "$path" in
                ALL|/usr/bin/"$cmd"|/bin/"$cmd"|/usr/sbin/"$cmd"|/sbin/"$cmd")
                    verdict="$verb" ;;
            esac
        done <<< "$rules"
        [ "$verdict" = allow ] || absent+=("$cmd")
    done
    echo "${absent[*]:-}"
}

if [ "$#" -eq 0 ]; then
    echo "Usage: $0 USER [USER...]" >&2
    exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "[!] check-sdwire-sudo.sh must run as root (to list other users' sudo rules); skipped"
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
    echo "    SD card flashing, updates and reads (MCP, CLI) fail for $user until a rule allows them."
    echo "    That grants root dd and mount to $user (effectively root); if you accept that, run:"
    echo "      ( sudo cat $RULE_FILE 2>/dev/null; echo '$(rule_for "$user")' ) > /tmp/labctl-sdwire.new"
    echo "      sudo visudo -cf /tmp/labctl-sdwire.new && sudo install -m 440 -o root -g root /tmp/labctl-sdwire.new $RULE_FILE"
done
if [ "$problems" -ne 0 ]; then
    echo "    See README.md, \"SDWire (SD Card Multiplexer)\", and issue #47."
fi
exit 0

# SPEC: SDWire Read-Access Operations

Specification for read-only file and filesystem inspection through
labctl, so agents and humans can check what's on an SBC's SD card
without falling back to direct `/dev/sdX` access.

**Status:** Proposed
**Source request:** SLM-OS capstone project, `pi-5-1` dual-boot +
EEPROM recovery session, 2026-04-22
**Target release:** v0.x

---

## Problem

`sdwire_update` gives agents a supervised **write** path to SD
cards — the agent prepares a local file and labctl handles the
mount-switch-unmount dance with verified device selection. For
**reading** card contents, no such path exists: to answer "what
does `autoboot.txt` currently say?" an agent must either:

1. Fall back to `sdwire_to_host` + direct `sudo mount /dev/sdX1` +
   `cat /mnt/.../autoboot.txt` + `umount` — the exact pattern
   `feedback_labctl_only.md` was written to prevent, because the
   `/dev/sdX` label is only loosely tied to the claimed SBC and
   collisions across lab SDWires have been observed.
2. Boot an OS on the SBC that can read the partition, then query
   it via SSH/serial — requires the SBC to actually boot, which
   is often the state you're trying to debug.

During the 2026-04-22 `pi-5-1` session this bit hard: ~10
read-only card inspections were done across:
- pre-edit state-of-the-world checks (before flipping
  `autoboot.txt`)
- post-edit verification (did the edit land?)
- file-consumption checks (did firstboot consume `ssh` /
  `userconf.txt`?)
- post-failure forensics (is `pieeprom.upd` left on the boot
  partition after the downgrade?)

Each one was a direct `/dev/sdX` mount. Every one of those mounts
is a future-footgun opportunity if an agent confuses device letters
between SDWires on a shared host.

---

## Proposed API

Three new MCP tools + CLI subcommands, all read-only, all going
through the same mount-switch-unmount path `sdwire_update` already
uses (just with the mount opened read-only):

### `sdwire_ls`

List directory contents on a partition of an SBC's SD card.

**Arguments:**
- `sbc_name` (required)
- `partition` (required, int)
- `path` (required, string, absolute within the partition,
  defaults to `/`)
- `recursive` (optional, bool, default `false`)
- `max_entries` (optional, int, default 1000 — safety cap for
  accidental `/usr` recursions)

**Output:**
JSON list of `{ name, type, size, mtime, mode }` entries.
`type` is `"file" | "dir" | "symlink" | "other"`. Bytes for size.
`mtime` as ISO-8601 UTC. `mode` as octal string (e.g. `"0755"`).

**Errors:**
- `partition_not_found` — no such partition
- `path_not_found` — path doesn't exist on the partition
- `fs_unsupported` — couldn't mount (e.g. corrupted or unknown FS)
- `entry_limit_exceeded` — directory larger than `max_entries`;
  output truncated with an `"_truncated": true` marker

### `sdwire_cat`

Read a file from a partition and return its contents.

**Arguments:**
- `sbc_name` (required)
- `partition` (required, int)
- `path` (required, string, absolute within the partition)
- `max_bytes` (optional, int, default 1 MB — safety cap for
  accidental multi-GB binaries)
- `encoding` (optional, `"text" | "base64" | "hex"`, default
  `"text"` — agents that need raw bytes should explicitly request
  `"base64"`)

**Output:**
- For `text`: the file contents as a UTF-8 string, plus `{ size,
  mtime, mode, truncated }` metadata. If the file isn't valid
  UTF-8, error with `binary_content` and suggest `base64`.
- For `base64` / `hex`: the encoded contents + metadata.

**Errors:**
- `path_not_found`
- `not_a_file` (caller asked to cat a directory/symlink)
- `size_limit_exceeded` — file larger than `max_bytes`; the call
  MUST return this rather than silently truncating, so agents
  see the boundary and raise the limit explicitly
- `binary_content` (when `encoding=text` but the file isn't UTF-8)

### `sdwire_info`

Partition-table and filesystem metadata for an SBC's SD card.

**Arguments:**
- `sbc_name` (required)

**Output:**
JSON summary:
```json
{
  "device_total_bytes": 31267487744,
  "disklabel_type": "msdos",
  "partitions": [
    {
      "num": 1, "start_mib": 1, "end_mib": 7630,
      "size_mib": 7629, "type": "fat32",
      "partuuid": "1d0c498d-01",
      "label": "SLMOS", "flags": ["lba"],
      "filesystem_uuid": "54D4-4272",
      "mount_status": "clean"
    },
    ...
  ],
  "free_space_regions": [
    { "start_mib": 0, "end_mib": 1, "size_mib": 1 },
    { "start_mib": 15252, "end_mib": 15253, "size_mib": 1 }
  ]
}
```

Sources the data from `parted unit MiB print free` + `blkid`.
Agents use this to answer "is there free space at the end of the
card?" and "what PARTUUID should `cmdline.txt` reference?" without
running `parted` themselves.

**Errors:**
- `read_failed` — device unreachable (SBC not powered off /
  SDWire not in host mode)

---

## Design choices

### Why a dedicated read API instead of a general "shell-out" tool

A tool like `sdwire_exec` that ran arbitrary commands inside a
labctl-managed mount would be more flexible but would re-open the
exact surface area the labctl-only rule exists to close: an
accidental `rm -rf /mnt/sdc1/*` is indistinguishable from a legit
read command at the wrapper layer. Dedicated read-only tools with
bounded outputs are boring by design, and "boring" is the point.

### Read-only mount

All three tools mount the partition with `ro,noatime,noexec,nosuid,
nodev` to make an accidental write impossible even if the tool's
code path has a bug.

### Output size caps

`sdwire_cat` defaulting to 1 MB and `sdwire_ls` defaulting to
`max_entries=1000` keep accidental `/var/log` cats from drowning
an agent's context with garbage. Agents that need more can raise
the cap explicitly.

### No globbing, no regex, no "find"

Keep the surface small. If an agent needs "list every .hef file",
they can `sdwire_ls --recursive` and filter locally. Glob
expansion in labctl is a shell-injection target and not worth the
complexity.

### Symlink handling in `sdwire_cat`

`sdwire_cat` should refuse to follow symlinks and return a
`symlink_target` field instead. Following symlinks blindly is how
you accidentally read `/proc/self/maps` on a mounted rootfs.

### Concurrency with existing ops

While one of these tools holds the card (host mode, partition
mounted), `sdwire_update` on the same SBC blocks. The existing
claim system handles who-owns-what at the SBC granularity; these
tools just need to serialize their own mount/unmount with
`sdwire_update`'s.

### Why NOT `sdwire_stat` / `sdwire_find` / `sdwire_du`

Considered and rejected for v1. `stat`'s output is fully covered
by `sdwire_ls` on the file's parent. `find` and `du` encourage
agents to script complex traversals through labctl when the
local-iteration-with-`sdwire_ls` path is clearer. If concrete
need surfaces for them later, add them then.

---

## Implementation notes

The existing `sdwire_update` flow inside labctl already does:

1. Verify SBC is powered off (or `force=True`)
2. `sdwire_to_host`
3. Probe for the block device assigned to this SBC's SDWire
4. Mount the requested partition (rw)
5. Do copies/renames/deletes
6. Unmount, `sdwire_to_dut`

Steps 1–3 and step 6 are identical for read ops. Step 4 becomes a
read-only mount (`mount -o ro,nosuid,nodev,noexec,noatime`), and
step 5 is replaced with a handler-per-tool:

- `sdwire_ls`: `os.scandir()` the target path; serialize entries.
- `sdwire_cat`: `open(..., "rb")`, read up to `max_bytes + 1`
  (caller sees `size_limit_exceeded` if the real file is larger),
  encode per `encoding`.
- `sdwire_info`: no partition mount needed — run `parted -s
  <device> unit MiB print free` + iterate each partition with
  `blkid` probe.

Step 6 runs in a `try/finally` so an exception in the handler
doesn't leave the card mounted on the host.

Python implementation can share the mount-context manager with
`sdwire_update` — extract it to an `sdwire_host_mount(partition,
mode="ro"|"rw")` helper used by both sides.

---

## MCP tool visibility

All three tools exposed as standard MCP tools alongside the
existing `sdwire_*` set. No new permission categories — they
require the same SBC claim the write tools do, so holding a claim
on `pi-5-1` lets you do read + write + power ops on that one
SBC and nothing else.

---

## Rollout plan

Recommended order, each usable standalone:

1. **`sdwire_info`** first — smallest surface, biggest marginal
   win (no other way to get partition table info). Can ship
   before the mount helper refactor since it doesn't mount.
2. **`sdwire_ls`** — needs the mount helper. Enables "is this
   file even there?" without the mount dance.
3. **`sdwire_cat`** — completes the read API. Biggest complexity
   is the encoding negotiation and symlink-refuse path.

Each ships with a test: flash a known layout to a test card,
call the tool, assert the output matches.

---

## Non-goals

- **Editing files through labctl.** `sdwire_update --copies` from
  a locally-prepared file is the pattern; agents should read with
  `sdwire_cat`, compute the new contents locally, write with
  `sdwire_update`. This keeps the "what did labctl write?" audit
  trail readable — every mutation is a file copy from a named
  local path.
- **Partition table manipulation** (`mkpart`, `resizepart`,
  `mkfs`). Separate spec if/when needed. Building a new card
  from scratch is still a "bypass labctl once, with care" flow
  today; wrapping it is a larger design exercise (what plan
  language? what dry-run semantics?) and can wait.
- **Live filesystem queries on a running SBC.** These tools work
  by switching SDWire to host mode — they require the SBC
  powered off. Live queries go through SSH/serial as they do today.

---

## Open questions

1. Should `sdwire_cat` on a `/proc`-like mount (if the agent
   somehow gets a live rootfs mounted) be prevented? I think the
   read-only mount of a detached card at rest handles this —
   `/proc`'s dynamic behavior only exists on a running kernel.
   But worth confirming in the implementation.

2. Should `max_bytes` / `max_entries` defaults be configurable
   per-agent? Probably not for v1 — single global defaults with
   explicit override on the call keep the tool schema small.

3. Encoding choice for `sdwire_cat`: is `"text"` auto-detect
   worth it, or should agents always pass `"base64"` for
   suspected-binary and `"text"` only for known-text files?
   Auto-detect (try UTF-8, fall back to reporting
   `binary_content`) feels like the right default.

# SPEC: Identity-Based Storage Device Guardrails

Specification for preventing direct raw-storage access to block devices
owned by labctl-managed SDWire workflows, while still allowing
operator use of `mount`, `dd`, `parted`, and related tools for other
removable media.

**Status:** Proposed
**Source request:** Follow-up to SDWire read-access design, 2026-04-22
**Target release:** v0.x

---

## Problem

The current operational model allows privileged storage commands such
as `mount`, `umount`, `dd`, `sync`, `partprobe`, `parted`, and `blkid`
for interactive work on SD cards that are **not** under SDWire control.
That flexibility is necessary for ad hoc tasks with USB programmers and
non-labctl media.

However, Linux block device names such as `/dev/sdb`, `/dev/sde`, and
`/dev/sdg` are nondeterministic. The same kernel-assigned path can mean:

1. an SD card routed through an SDWire assigned to a specific SBC and
   expected to be manipulated only through labctl, or
2. an unrelated removable card inserted through a generic USB reader and
   expected to be manipulated with normal storage tools.

This makes any policy of "forbid `/dev/sde`" incorrect by construction.
The protection boundary must be based on **device identity**, not on
the transient `/dev/sdX` node.

---

## Goals

- Prevent direct raw-storage access to SDWire-managed block devices
  outside labctl.
- Preserve operator ability to use raw storage tools on non-SDWire
  removable media.
- Base policy decisions on stable hardware identity, not kernel device
  naming.
- Make the protection understandable and inspectable by operators.
- Keep labctl as the only approved path for SDWire-owned media.

## Non-goals

- Prevent unrestricted `root` from touching any device whatsoever.
- Build a general-purpose sandbox for arbitrary shell commands.
- Protect against a user who can run unrestricted `sudo bash`,
  interpreters, or arbitrary root commands.
- Replace existing labctl SDWire read/write flows.

---

## Proposed model

### High-level approach

Introduce an identity-based guardrail layer with three parts:

1. **Device classification**
   Use udev/sysfs properties to determine whether a block device belongs
   to a labctl-managed SDWire.
2. **Protected-device tagging**
   Mark SDWire-backed block devices with a stable property or symlink,
   independent of their `/dev/sdX` name.
3. **Privileged wrapper commands**
   Stop granting direct `sudo mount`, `sudo dd`, `sudo parted`, etc.
   Instead, grant `sudo` access only to wrapper commands that reject
   SDWire-protected devices and permit other removable media.

### Why wrapper commands are required

If operators retain direct `sudo mount /dev/sdX ...` and
`sudo dd of=/dev/sdX ...`, labctl cannot enforce any identity-based
policy. The only robust enforcement point is a root-owned wrapper that:

- canonicalizes the requested device,
- resolves its stable identity via udev/sysfs,
- rejects it if it is labctl-protected,
- otherwise executes the requested storage action.

---

## Device identity source

The classification decision MUST use stable hardware identity, not
the kernel block name.

Candidate identity sources, in preferred order:

1. udev properties from `udevadm info --query=property --name=/dev/sdX`
2. sysfs parent-chain attributes under `/sys/class/block/<dev>/device/...`
3. `/dev/disk/by-id/*`
4. `/dev/disk/by-path/*`

The implementation SHOULD support SDWire-backed media detection using
one or more of:

- USB serial number of the SDWire device
- USB vendor/product pair combined with serial
- stable USB topology path for the SDWire reader
- explicit mapping from SDWire registration to a known by-id/by-path form

The system MUST NOT rely only on `/dev/sdX` or partition numbers.

---

## Protected-device tagging

When the kernel exposes a block device whose parent hardware matches a
registered SDWire, udev should tag it as labctl-protected.

### Required behavior

For a protected whole-disk device and its partitions, the system SHOULD
expose at least one of:

- `ENV{LABCTL_PROTECTED}="1"`
- `ENV{LABCTL_OWNER}="<sbc-name>"`
- `ENV{LABCTL_SDWIRE}="<sdwire-name-or-serial>"`

and MAY also create stable symlinks such as:

- `/dev/labctl-protected/<sbc-name>`
- `/dev/labctl-protected/<sbc-name>-part1`

### Scope

Protection applies to:

- the whole-disk device, e.g. `/dev/sde`
- all partitions of that device, e.g. `/dev/sde1`, `/dev/sde2`

### Failure posture

If device identity cannot be determined with confidence, the guard
should fail closed for SDWire detection only when the device strongly
matches a registered SDWire. Otherwise, the wrapper may allow access
but MUST record enough audit detail to show the classification basis.

---

## Privileged wrapper interface

Introduce a new root-owned CLI surface for generic storage operations on
non-SDWire removable media.

Working name:

- `labctl storage mount`
- `labctl storage umount`
- `labctl storage dd`
- `labctl storage parted`
- `labctl storage blkid`
- `labctl storage probe`

Alternative acceptable shape:

- `/usr/local/libexec/labctl-storage mount ...`
- `/usr/local/bin/labctl-storage mount ...`

The exact command surface is an implementation choice; the key point is
that privileged storage access flows through a labctl-controlled wrapper.

### Wrapper requirements

The wrapper MUST:

- accept a device argument or parse one from the requested operation,
- resolve symlinks and canonicalize the target block device,
- determine the parent whole-disk device for partition targets,
- inspect the device's udev/sysfs identity,
- reject devices tagged `LABCTL_PROTECTED=1`,
- emit a clear error that the device is SDWire-managed and must be
  accessed via labctl,
- allow execution for non-protected devices,
- audit all allow/deny decisions.

The wrapper SHOULD:

- print the stable identity it used for the decision,
- print the owning SBC / SDWire when denying access,
- support a `--explain` mode for dry-run classification.

---

## Sudo policy

### Current issue

A broad sudoers rule allowing direct access to `mount`, `umount`, `dd`,
`sync`, `partprobe`, and similar commands bypasses any classification
logic entirely.

### Proposed policy

Operators should no longer receive direct passwordless sudo for raw
storage commands. Instead, they receive passwordless sudo only for the
approved wrapper command set.

Allowed:

- `sudo labctl storage mount ...`
- `sudo labctl storage dd ...`
- `sudo labctl storage parted ...`

Not allowed directly:

- `sudo mount ...`
- `sudo umount ...`
- `sudo dd ...`
- `sudo parted ...`
- `sudo blkid ...`
- `sudo bash`
- `sudo python ...`

### Important constraint

This feature only works as intended if users do **not** have broad
root shells or arbitrary-command sudo access elsewhere.

---

## Operator UX

### Successful non-protected access

Example:

```text
$ sudo labctl storage mount /dev/sdg1 /mnt/card
Allowed: /dev/sdg1 classified as non-SDWire removable media
Mounted /dev/sdg1 at /mnt/card
```

### Rejected protected access

Example:

```text
$ sudo labctl storage mount /dev/sde1 /mnt/card
Denied: /dev/sde1 belongs to SDWire-managed media
Owner: pi-5-1
SDWire: sdwire-3 (serial bdgrd_sdwirec_522)
Use: labctl sdwire ...
```

### Explain mode

Example:

```text
$ labctl storage explain /dev/sde
device: /dev/sde
whole_disk: /dev/sde
classification: protected
basis:
  - LABCTL_PROTECTED=1
  - by-path=/dev/disk/by-path/pci-0000:00:14.0-usb-0:2.3:1.0-scsi-0:0:0:0
  - sdwire_serial=bdgrd_sdwirec_522
owner_sbc: pi-5-1
```

---

## Audit requirements

Every wrapper invocation MUST create an audit entry with:

- actor
- timestamp
- requested operation
- requested device path
- canonical device path
- classification result: `allowed` or `denied`
- classification basis
- owning SBC / SDWire when known
- executed command on success

Denied operations are especially important and MUST be recorded.

---

## Interaction with labctl SDWire features

This feature is complementary to, not a replacement for:

- `sdwire_to_host`
- `sdwire_to_dut`
- `sdwire_update`
- `flash_image`
- `sdwire_ls`
- `sdwire_cat`
- `sdwire_info`

Labctl remains the only supported interface for SDWire-managed media.
The wrapper exists only for non-SDWire removable storage workflows.

---

## Design choices

### Why not blacklist `/dev/sdX`

Because `/dev/sdX` names are nondeterministic and can refer to
different physical devices across boots and insertions.

### Why not rely on Unix file permissions alone

Permissions on `/dev/sdX` can help for non-root users, but they do not
solve the privileged-access problem and still key off unstable device
names unless combined with identity-aware wrappers.

### Why not attempt to block unrestricted root

That is not technically robust in this environment. The practical
security boundary is "users with constrained sudo may use approved
wrappers only."

### Why not make generic wrappers operate on protected devices with a flag

That would recreate the footgun this feature exists to remove. The
escape hatch should be labctl's SDWire-specific workflows, not a generic
`--force` on raw storage tools.

---

## Implementation notes

Likely implementation pieces:

1. Add a resolver that maps registered SDWire devices to stable host
   identity attributes.
2. Add udev rules that tag matching block devices and partitions.
3. Add a `labctl storage` wrapper command family.
4. Add audit logging for allow/deny classification results.
5. Update installation docs and sudoers guidance to remove direct raw
   command allowances in favor of wrapper allowances.

The udev rule and wrapper logic MUST be designed so partitions inherit
the protection decision from their parent disk.

---

## Open questions

1. What is the most reliable SDWire identity source across both
   `sdwire` and `sdwirec` hardware: USB serial, by-path, or a hybrid?
2. Should the wrapper surface be a new `labctl storage ...` family or a
   separate helper binary with a narrower parsing surface?
3. Do we want `labctl storage explain <device>` in v1, or only after the
   enforcement path is working?
4. Should unclassified removable media default to allowed with audit, or
   should enforcement be limited strictly to positively identified
   SDWire-backed media?

---

## Acceptance criteria

- An SDWire-managed card appearing as any `/dev/sdX` is rejected by the
  generic privileged storage wrapper.
- A non-SDWire removable card appearing as that same `/dev/sdX` at a
  different time is allowed by the wrapper.
- Denials identify the owning SBC / SDWire when available.
- Direct raw storage sudo permissions are replaced by wrapper-only
  permissions in documented setup guidance.
- Audit logs capture both allowed and denied access attempts.

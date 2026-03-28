#!/usr/bin/env python3
"""
Debug script for direct Kasa smart strip/plug control.

Usage:
    kasa-debug.py <host> <action> [options]

Examples:
    # Discover device and show info
    kasa-debug.py 192.168.4.140 info

    # Test authentication and show hash diagnostics
    kasa-debug.py 192.168.4.140 test-auth -u email@example.com -p password

    # Turn on outlet 4 with credentials
    kasa-debug.py 192.168.4.140 on -i 4 -u email@example.com -p password

    # Get status of all outlets
    kasa-debug.py 192.168.4.140 status -u email@example.com -p password

    # Verbose mode for full debug output
    kasa-debug.py 192.168.4.140 info -u email@example.com -p password -v
"""

import argparse
import asyncio
import hashlib
import logging
import sys
import time


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()


def _sha1(data: bytes) -> bytes:
    return hashlib.sha1(data).digest()


def _sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def show_auth_diagnostics(username: str, password: str, device_owner: str):
    """Show credential hashing diagnostics to help debug auth issues."""
    print("\n--- Authentication Diagnostics ---")
    print(f"Username:        {username!r}")
    print(f"  length:        {len(username)}")
    print(f"  bytes:         {username.encode()!r}")
    print(f"Password:        {'*' * len(password)!r}")
    print(f"  length:        {len(password)}")
    print(f"  bytes:         {password.encode()!r}")
    print(f"  has non-ASCII: {any(ord(c) > 127 for c in password)}")

    # Owner hash (md5 of username) — used to match device owner field
    owner_hash = _md5(username.encode()).hex().upper()
    print(f"\nOwner hash (md5 of username):")
    print(f"  Computed:      {owner_hash}")
    print(f"  Device:        {device_owner}")
    print(f"  Match:         {owner_hash == device_owner.upper()}")

    # KLAP v1 auth hash: md5(md5(username) + md5(password))
    v1_hash = _md5(_md5(username.encode()) + _md5(password.encode()))
    print(f"\nKLAP v1 auth hash (md5(md5(user) + md5(pass))):")
    print(f"  {v1_hash.hex()}")

    # KLAP v2 auth hash: sha256(sha1(username) + sha1(password))
    v2_hash = _sha256(_sha1(username.encode()) + _sha1(password.encode()))
    print(f"\nKLAP v2 auth hash (sha256(sha1(user) + sha1(pass))):")
    print(f"  {v2_hash.hex()}")

    # Show what default credentials produce
    print("\n--- Default Credential Hashes ---")
    defaults = {
        "KASA default": ("kasa@tp-link.net", "kasaSetup"),
        "TAPO default": ("test@tp-link.net", "test"),
        "Blank": ("", ""),
    }
    for label, (du, dp) in defaults.items():
        do = _md5(du.encode()).hex().upper()
        dv1 = _md5(_md5(du.encode()) + _md5(dp.encode())).hex()
        dv2 = _sha256(_sha1(du.encode()) + _sha1(dp.encode())).hex()
        owner_match = " <-- OWNER MATCH" if do == device_owner.upper() else ""
        print(f"\n  {label} ({du!r} / {dp!r}):")
        print(f"    Owner:   {do}{owner_match}")
        print(f"    KLAP v1: {dv1}")
        print(f"    KLAP v2: {dv2}")


async def discover_device(args, log):
    """Discover device and return it, or None on failure."""
    import kasa

    kwargs = {"host": args.host}

    credentials = None
    if args.username and args.password:
        credentials = kasa.Credentials(args.username, args.password)
        kwargs["credentials"] = credentials
        log.debug("Using credentials for user: %s", args.username)
    else:
        log.debug("No credentials provided (may fail on KLAP devices)")

    if args.timeout:
        kwargs["timeout"] = args.timeout

    log.info("Discovering device at %s ...", args.host)
    t0 = time.monotonic()

    try:
        device = await kasa.Discover.discover_single(**kwargs)
    except kasa.AuthenticationError as e:
        log.error("Authentication failed during discovery: %s", e)
        print(f"\nAuthentication error: {e}", file=sys.stderr)
        return None
    except kasa.KasaException as e:
        log.error("Discovery failed: %s: %s", type(e).__name__, e)
        print(f"\nDiscovery error: {e}", file=sys.stderr)
        return None

    elapsed = time.monotonic() - t0
    log.info("Discovery completed in %.2fs", elapsed)
    log.debug("Device class: %s", type(device).__name__)
    log.debug("Device ID: %s", getattr(device, "device_id", "N/A"))

    return device


async def run_test_auth(args):
    """Test authentication and show detailed hash diagnostics."""
    import kasa

    log = logging.getLogger("kasa-debug")

    # First do a raw discovery to get device info without auth
    print(f"Discovering {args.host} ...")
    raw_kwargs = {"host": args.host}
    if args.timeout:
        raw_kwargs["timeout"] = args.timeout

    try:
        device = await kasa.Discover.discover_single(**raw_kwargs)
    except kasa.KasaException as e:
        print(f"Discovery failed: {e}", file=sys.stderr)
        return 1

    # Get discovery info
    disc_info = getattr(device, "_discovery_info", {})
    result = disc_info.get("result", disc_info)
    device_owner = result.get("owner", "unknown")
    encrypt_type = result.get("mgt_encrypt_schm", {}).get("encrypt_type", "unknown")
    factory_default = result.get("factory_default", "unknown")
    model = result.get("device_model", "unknown")

    print(f"\nDevice:          {model}")
    print(f"Encrypt type:    {encrypt_type}")
    print(f"Factory default: {factory_default}")
    print(f"Device owner:    {device_owner}")

    if args.username and args.password:
        show_auth_diagnostics(args.username, args.password, device_owner)
    else:
        print("\nNo credentials provided (-u / -p). Showing default hashes only.")
        show_auth_diagnostics("", "", device_owner)

    # Now try actual authentication
    print("\n--- Authentication Test ---")

    creds_to_try = []
    if args.username and args.password:
        creds_to_try.append(
            ("Provided credentials", args.username, args.password)
        )
    creds_to_try.extend([
        ("KASA default", "kasa@tp-link.net", "kasaSetup"),
        ("TAPO default", "test@tp-link.net", "test"),
        ("Blank", "", ""),
    ])

    for label, un, pw in creds_to_try:
        masked_pw = "*" * len(pw) if pw else "(empty)"
        print(f"\nTrying {label} ({un!r} / {masked_pw}) ...")

        test_kwargs = {"host": args.host}
        if un or pw:
            test_kwargs["credentials"] = kasa.Credentials(un, pw)
        if args.timeout:
            test_kwargs["timeout"] = args.timeout

        try:
            test_device = await kasa.Discover.discover_single(**test_kwargs)
            await test_device.update()
            print(f"  SUCCESS! Authenticated as {un!r}")
            print(f"  Device alias: {test_device.alias}")
            print(f"  Is on: {test_device.is_on}")
            if test_device.children:
                print(f"  Outlets: {len(test_device.children)}")
                for i, child in enumerate(test_device.children):
                    state = "ON" if child.is_on else "OFF"
                    print(f"    [{i + 1}] {child.alias:30s} {state}")
            await test_device.disconnect()
            return 0
        except kasa.AuthenticationError:
            print(f"  FAILED (auth error)")
            try:
                await test_device.disconnect()
            except Exception:
                pass
        except kasa.KasaException as e:
            print(f"  FAILED ({type(e).__name__}: {e})")
            try:
                await test_device.disconnect()
            except Exception:
                pass

    print("\nAll credential combinations failed.", file=sys.stderr)
    return 1


async def run(args):
    import kasa

    if args.action == "test-auth":
        return await run_test_auth(args)

    log = logging.getLogger("kasa-debug")

    device = await discover_device(args, log)
    if device is None:
        return 1

    # Update device state
    log.info("Fetching device state ...")
    t0 = time.monotonic()

    try:
        await device.update()
    except kasa.AuthenticationError as e:
        log.error("Authentication failed during update: %s", e)
        print(f"\nAuthentication error: {e}", file=sys.stderr)
        print(
            "Use -u and -p with your TP-Link cloud account (Kasa app login).",
            file=sys.stderr,
        )
        await device.disconnect()
        return 1
    except kasa.KasaException as e:
        log.error("Update failed: %s: %s", type(e).__name__, e)
        print(f"\nDevice update error: {e}", file=sys.stderr)
        await device.disconnect()
        return 1

    elapsed = time.monotonic() - t0
    log.info("State update completed in %.2fs", elapsed)

    # Device info
    print(f"\nDevice:     {device.alias}")
    print(f"Model:      {device.model}")
    print(f"Host:       {device.host}")
    print(f"Type:       {device.device_type}")
    print(f"HW Version: {getattr(device, 'hw_info', {}).get('hw_ver', 'N/A')}")
    print(f"FW Version: {getattr(device, 'hw_info', {}).get('sw_ver', 'N/A')}")
    print(f"Is On:      {device.is_on}")
    print(f"Children:   {len(device.children) if device.children else 0}")

    if device.children:
        print(f"\nOutlets ({len(device.children)}):")
        for i, child in enumerate(device.children):
            state = "ON" if child.is_on else "OFF"
            print(f"  [{i + 1}] {child.alias:30s} {state}")

    # If action is just info or status, we're done
    if args.action in ("info", "status"):
        await device.disconnect()
        return 0

    # For on/off/cycle, determine target
    target = device
    target_name = device.alias

    if device.children:
        if not args.index:
            print(
                f"\nError: Device has {len(device.children)} outlets. "
                f"Use -i/--index to select one (1-{len(device.children)}).",
                file=sys.stderr,
            )
            await device.disconnect()
            return 1

        idx = args.index - 1
        if idx < 0 or idx >= len(device.children):
            print(
                f"\nError: Index {args.index} out of range "
                f"(1-{len(device.children)}).",
                file=sys.stderr,
            )
            await device.disconnect()
            return 1

        target = device.children[idx]
        target_name = f"{device.alias} outlet {args.index} ({target.alias})"

    try:
        if args.action == "on":
            log.info("Sending turn_on to %s", target_name)
            await target.turn_on()
            print(f"\nPower ON: {target_name}")

        elif args.action == "off":
            log.info("Sending turn_off to %s", target_name)
            await target.turn_off()
            print(f"\nPower OFF: {target_name}")

        elif args.action == "cycle":
            delay = args.delay
            log.info("Power cycling %s (delay: %.1fs)", target_name, delay)
            await target.turn_off()
            print(f"\nPower OFF: {target_name}")
            print(f"Waiting {delay}s ...")
            await asyncio.sleep(delay)
            await target.turn_on()
            print(f"Power ON: {target_name}")

    except kasa.AuthenticationError as e:
        log.error("Authentication failed during %s: %s", args.action, e)
        print(f"\nAuthentication error: {e}", file=sys.stderr)
        return 1
    except kasa.KasaException as e:
        log.error("%s failed: %s: %s", args.action, type(e).__name__, e)
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    finally:
        await device.disconnect()

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Debug script for Kasa smart strip/plug control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  %(prog)s 192.168.4.140 info
  %(prog)s 192.168.4.140 test-auth -u email -p pass
  %(prog)s 192.168.4.140 status -u email -p pass
  %(prog)s 192.168.4.140 on -i 4 -u email -p pass
  %(prog)s 192.168.4.140 off -i 4 -u email -p pass -v
  %(prog)s 192.168.4.140 cycle -i 4 -u email -p pass --delay 3
""",
    )

    parser.add_argument("host", help="Device IP address or hostname")
    parser.add_argument(
        "action",
        choices=["info", "status", "on", "off", "cycle", "test-auth"],
        help="Action to perform",
    )
    parser.add_argument(
        "-i", "--index", type=int, help="Outlet index (1-based, required for strips)"
    )
    parser.add_argument("-u", "--username", help="TP-Link cloud account email")
    parser.add_argument("-p", "--password", help="TP-Link cloud account password")
    parser.add_argument(
        "-t", "--timeout", type=int, default=10, help="Connection timeout in seconds (default: 10)"
    )
    parser.add_argument(
        "--delay", type=float, default=2.0, help="Delay for power cycle in seconds (default: 2.0)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose debug output"
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    # Also enable python-kasa internal logging in verbose mode
    if args.verbose:
        logging.getLogger("kasa").setLevel(logging.DEBUG)

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()

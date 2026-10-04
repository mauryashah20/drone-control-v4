#!/usr/bin/env python3
"""
Ground Station Drone Discovery Lookup Utility.

Discovers the current dynamic IPv6 address of a drone from the Vercel registry.
Can be used standalone via CLI or imported as a module by ground station apps.

REMINDER: This script only queries the Vercel registry for the IP.
Telemetry, MAVLink, and video will connect DIRECTLY to the returned IPv6.
"""

import os
import sys
import json
import argparse
from typing import Optional, Dict, Any

try:
    import requests
except ImportError:
    print("[ERROR] 'requests' library is required. Run: pip install requests")
    sys.exit(1)


def lookup_drone(
    registry_url: str,
    device_id: str,
    timeout: float = 5.0,
) -> Optional[Dict[str, Any]]:
    """
    Queries the Vercel registry for the given drone's current IPv6 address and status.

    Returns:
        dict with keys { "deviceId", "ipv6", "port", "lastSeen", "isOnline", ... }
        or None if device not found / error.
    """
    url = f"{registry_url.rstrip('/')}/api/lookup"
    params = {"deviceId": device_id}

    try:
        resp = requests.get(url, params=params, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            print(f"[LOOKUP] Device '{device_id}' was not found in registry (HTTP 404).", file=sys.stderr)
            return None
        else:
            print(f"[LOOKUP ERROR] HTTP {resp.status_code}: {resp.text}", file=sys.stderr)
            return None
    except requests.RequestException as e:
        print(f"[LOOKUP NETWORK ERROR] Could not reach registry at {registry_url}: {e}", file=sys.stderr)
        return None


def get_drone_direct_target(
    registry_url: str,
    device_id: str,
    require_online: bool = True,
) -> Optional[tuple]:
    """
    High-level helper for ground control scripts.
    Returns (ipv6_address, port) if drone is discovered and online.
    """
    info = lookup_drone(registry_url, device_id)
    if not info:
        return None

    if require_online and not info.get("isOnline"):
        print(
            f"[WARNING] Drone '{device_id}' is marked OFFLINE "
            f"(last seen {info.get('secondsSinceLastSeen')}s ago).",
            file=sys.stderr,
        )
        return None

    ipv6 = info.get("ipv6")
    port = info.get("port", 14550)
    if not ipv6:
        print(f"[ERROR] Drone '{device_id}' has no registered IPv6 address.", file=sys.stderr)
        return None

    return (ipv6, port)


def main():
    parser = argparse.ArgumentParser(description="Query Drone Discovery Registry for Current IPv6")
    parser.add_argument("--url", default=os.getenv("REGISTRY_URL"), help="Vercel Registry base URL")
    parser.add_argument("--id", default=os.getenv("DEVICE_ID", "DRONE-001"), help="Target Device ID")
    parser.add_argument("--json", action="store_true", help="Output raw JSON response")
    parser.add_argument("--ip-only", action="store_true", help="Output only the IPv6 string (useful in shell scripts)")

    args = parser.parse_args()

    if not args.url:
        print("[ERROR] Must specify --url or set REGISTRY_URL environment variable.", file=sys.stderr)
        sys.exit(1)

    result = lookup_drone(args.url, args.id)
    if not result:
        sys.exit(2)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    if args.ip_only:
        print(result.get("ipv6", ""))
        return

    # Formatted terminal display
    online_symbol = "🟢 ONLINE" if result.get("isOnline") else "🔴 OFFLINE"
    print("\n=======================================================")
    print("      DRONE DISCOVERY LOOKUP RESULT")
    print("=======================================================")
    print(f"  Device ID:          {result.get('deviceId')}")
    print(f"  Status:             {online_symbol}")
    print(f"  Direct IPv6:        {result.get('ipv6')}")
    print(f"  Target Port:        {result.get('port')}")
    print(f"  Last Seen:          {result.get('lastSeen')}")
    print(f"  Age:                {result.get('secondsSinceLastSeen')} seconds ago")
    print("=======================================================")
    print(f"\nDirect connection string for GStreamer / MAVLink:")
    print(f"  [{result.get('ipv6')}]:{result.get('port')}\n")


if __name__ == "__main__":
    main()

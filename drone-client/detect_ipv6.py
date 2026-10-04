#!/usr/bin/env python3
"""
IPv6 Detection Utility for Raspberry Pi / Cellular LTE Modem.

Extracts the public globally-routable IPv6 address assigned by the cellular carrier.
Filters out link-local (fe80::), loopback (::1), and private ULA (fc00::/7).
"""

import subprocess
import socket
import re
import urllib.request
import logging
from typing import Optional, List, Tuple

logger = logging.getLogger("detect_ipv6")


def is_global_unicast_ipv6(addr: str) -> bool:
    """Check if address is a valid globally routable IPv6 address."""
    addr = addr.strip().lower()
    if not addr or ":" not in addr:
        return False
    # Exclude loopback and unspecified
    if addr in ("::1", "::"):
        return False
    # Exclude link-local (fe80::/10)
    if addr.startswith("fe8") or addr.startswith("fe9") or addr.startswith("fea") or addr.startswith("feb"):
        return False
    # Exclude unique local / private (fc00::/7)
    if addr.startswith("fc") or addr.startswith("fd"):
        return False
    # Exclude multicast (ff00::/8)
    if addr.startswith("ff"):
        return False
    # Exclude documentation / discard
    if addr.startswith("2001:db8:") or addr.startswith("100::"):
        return False

    # Must parse successfully as IPv6
    try:
        socket.inet_pton(socket.AF_INET6, addr)
        return True
    except (socket.error, OSError):
        return False


def get_local_interfaces_ipv6() -> List[Tuple[str, str, str]]:
    """
    Parses 'ip -6 addr show' on Linux to extract interface names, addresses, and flags.
    Returns list of (interface_name, ipv6_address, scope/flags).
    """
    results = []
    try:
        out = subprocess.check_output(["ip", "-6", "addr", "show"], stderr=subprocess.DEVNULL, universal_newlines=True)
        current_iface = ""
        for line in out.splitlines():
            line_str = line.strip()
            # New interface line: "2: wwan0: <POINTOPOINT,MULTICAST,NOARP,UP,LOWER_UP> ..."
            m_iface = re.match(r"^\d+:\s+([a-zA-Z0-9_\-\.]+):", line_str)
            if m_iface:
                current_iface = m_iface.group(1)
                continue

            # Address line: "inet6 2401:4900:.../64 scope global dynamic noprefixroute"
            if line_str.startswith("inet6 "):
                parts = line_str.split()
                if len(parts) >= 2:
                    raw_addr = parts[1].split("/")[0]
                    flags = " ".join(parts[2:])
                    results.append((current_iface, raw_addr, flags))
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return results


def detect_cellular_ipv6(preferred_iface: Optional[str] = None) -> Optional[str]:
    """
    Detects the primary globally routable IPv6 address from network interfaces.
    Prioritizes LTE/WWAN modem interfaces (wwan0, usb0, rmnet*, ppp*).
    """
    ifaces = get_local_interfaces_ipv6()
    if not ifaces:
        logger.debug("No IPv6 addresses found via local 'ip -6' command")
        return None

    # Priority 1: User explicitly configured interface
    if preferred_iface:
        for iface, addr, flags in ifaces:
            if iface == preferred_iface and is_global_unicast_ipv6(addr):
                # Prefer dynamic non-temporary or primary global address
                if "temporary" not in flags.lower():
                    return addr
        # Fallback to any global address on that interface
        for iface, addr, _ in ifaces:
            if iface == preferred_iface and is_global_unicast_ipv6(addr):
                return addr

    # Priority 2: Known cellular/modem interface names
    cellular_prefixes = ("wwan", "rmnet", "usb", "cdc", "ppp", "qmi", "eth1")
    for pref in cellular_prefixes:
        for iface, addr, flags in ifaces:
            if iface.startswith(pref) and is_global_unicast_ipv6(addr):
                if "temporary" not in flags.lower():
                    logger.info(f"Detected cellular IPv6 on {iface}: {addr}")
                    return addr

    # Priority 3: Any non-temporary global IPv6 address
    for iface, addr, flags in ifaces:
        if is_global_unicast_ipv6(addr) and "temporary" not in flags.lower() and iface != "lo":
            logger.info(f"Detected global IPv6 on {iface}: {addr}")
            return addr

    # Priority 4: Any global IPv6 address at all
    for iface, addr, _ in ifaces:
        if is_global_unicast_ipv6(addr) and iface != "lo":
            return addr

    return None


def fetch_public_ipv6_fallback(timeout: float = 3.0) -> Optional[str]:
    """
    Queries an external IPv6-only echo endpoint as a fallback if local interface
    parsing cannot determine the public address.
    """
    endpoints = [
        "https://api64.ipify.org",
        "https://ipv6.icanhazip.com",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7.68.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ip = resp.read().decode("utf-8").strip()
                if is_global_unicast_ipv6(ip):
                    return ip
        except Exception:
            continue
    return None


def get_best_ipv6(preferred_iface: Optional[str] = None) -> Optional[str]:
    """Top-level resolver: tries local modem interfaces first, then falls back to echo."""
    ip = detect_cellular_ipv6(preferred_iface)
    if ip:
        return ip
    return fetch_public_ipv6_fallback()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Scanning interfaces for global IPv6...")
    for iface, addr, flags in get_local_interfaces_ipv6():
        print(f"  {iface:10} -> {addr} ({flags}) [Valid Global: {is_global_unicast_ipv6(addr)}]")
    best = get_best_ipv6()
    print(f"\nBest detected globally routable IPv6: {best or 'None found'}")

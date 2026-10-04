#!/usr/bin/env python3
"""
Drone Registration Daemon for Raspberry Pi.

Periodically detects the drone's public IPv6 address on LTE and keeps
the Vercel discovery registry updated. If the cellular connection drops
and gets reassigned a new IPv6 prefix, this daemon immediately re-registers
the new address.

NOTE: This daemon communicates ONLY with the Vercel discovery endpoint over HTTPS.
It NEVER touches or relays telemetry/video data.
"""

import os
import sys
import time
import json
import logging
import argparse
from typing import Optional

try:
    import requests
except ImportError:
    print("[ERROR] 'requests' library is required. Install with: pip install requests")
    sys.exit(1)

from detect_ipv6 import get_best_ipv6

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [DroneRegistry] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("DroneRegistry")


class DroneRegistrationClient:
    def __init__(
        self,
        registry_url: str,
        device_id: str,
        device_token: str,
        port: int = 14550,
        poll_interval: int = 15,
        preferred_iface: Optional[str] = None,
        registration_secret: Optional[str] = None,
    ):
        self.registry_url = registry_url.rstrip("/")
        self.device_id = device_id
        self.device_token = device_token
        self.port = port
        self.poll_interval = max(5, poll_interval)
        self.preferred_iface = preferred_iface
        self.registration_secret = registration_secret

        self.current_ipv6: Optional[str] = None
        self.registered = False
        self.consecutive_failures = 0

    @property
    def auth_headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.device_token}",
            "Content-Type": "application/json",
            "User-Agent": f"DroneClient/{self.device_id}",
        }
        if self.registration_secret:
            headers["x-registration-secret"] = self.registration_secret
        return headers

    def register(self, ipv6: str) -> bool:
        """Sends full registration payload with current IPv6 and port."""
        endpoint = f"{self.registry_url}/api/register"
        payload = {
            "deviceId": self.device_id,
            "ipv6": ipv6,
            "port": self.port,
            "token": self.device_token,
        }

        try:
            logger.info(f"Registering with {endpoint} -> IP: {ipv6}, Port: {self.port}")
            resp = requests.post(endpoint, json=payload, headers=self.auth_headers, timeout=10)
            data = resp.json()

            if resp.status_code == 200 and data.get("success"):
                logger.info(
                    f"[SUCCESS] Registered {self.device_id} -> IPv6: {ipv6} | "
                    f"Source IP Match: {data.get('sourceIpMatches')} "
                    f"(Edge IP: {data.get('observedEdgeIp', 'N/A')})"
                )
                self.current_ipv6 = ipv6
                self.registered = True
                self.consecutive_failures = 0
                return True
            else:
                logger.error(f"[FAILED] HTTP {resp.status_code}: {data.get('error', resp.text)}")
                return False
        except requests.RequestException as e:
            logger.error(f"[NETWORK ERROR] Failed to connect to registry: {e}")
            return False

    def heartbeat(self) -> bool:
        """Sends lightweight heartbeat to update last_seen timestamp."""
        endpoint = f"{self.registry_url}/api/heartbeat"
        payload = {
            "deviceId": self.device_id,
            "token": self.device_token,
        }

        try:
            resp = requests.post(endpoint, json=payload, headers=self.auth_headers, timeout=8)
            data = resp.json()
            if resp.status_code == 200 and data.get("success"):
                logger.debug(f"[HEARTBEAT] OK for {self.device_id}")
                self.consecutive_failures = 0
                return True
            else:
                logger.warning(f"[HEARTBEAT FAILED] HTTP {resp.status_code}: {data.get('error', resp.text)}")
                # If server says device not found, mark unregistered so next cycle registers
                if resp.status_code == 404:
                    self.registered = False
                return False
        except requests.RequestException as e:
            logger.warning(f"[HEARTBEAT ERROR] Network failure: {e}")
            return False

    def run_forever(self):
        """Main loop: monitors IPv6 changes and pulses heartbeat."""
        logger.info(f"Starting Drone Registration Daemon for Device: {self.device_id}")
        logger.info(f"Registry URL: {self.registry_url}")
        logger.info(f"Check Interval: {self.poll_interval}s")

        while True:
            try:
                # 1. Detect current global IPv6
                detected_ip = get_best_ipv6(self.preferred_iface)

                if not detected_ip:
                    logger.warning("[NO IP] No global IPv6 found on cellular/local interfaces. Waiting for LTE connection...")
                    time.sleep(min(15, self.poll_interval))
                    continue

                # 2. Check if IP changed or first registration
                if not self.registered or detected_ip != self.current_ipv6:
                    if detected_ip != self.current_ipv6 and self.current_ipv6 is not None:
                        logger.warning(
                            f"[IP CHANGED] Previous: {self.current_ipv6} -> New: {detected_ip}! Updating registry immediately..."
                        )

                    success = self.register(detected_ip)
                    if not success:
                        self.consecutive_failures += 1
                else:
                    # IP unchanged, pulse lightweight heartbeat
                    success = self.heartbeat()
                    if not success:
                        self.consecutive_failures += 1

                # Dynamic sleep with backoff on repeated errors
                sleep_time = self.poll_interval
                if self.consecutive_failures > 3:
                    # Cellular might be dead or reconnecting, wait a bit longer to prevent rapid looping
                    sleep_time = min(60, self.poll_interval * 2)

                time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("Daemon stopped by user.")
                break
            except Exception as e:
                logger.exception(f"Unexpected error in daemon loop: {e}")
                time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description="Drone IPv6 Registration Daemon")
    parser.add_argument("--url", default=os.getenv("REGISTRY_URL"), help="Vercel Registry base URL (e.g. https://drone-reg.vercel.app)")
    parser.add_argument("--id", default=os.getenv("DEVICE_ID", "DRONE-001"), help="Device ID")
    parser.add_argument("--token", default=os.getenv("DEVICE_TOKEN"), help="Device secret token")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "14550")), help="Listening port (e.g. 14550 for MAVLink)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "15")), help="Heartbeat interval in seconds")
    parser.add_argument("--iface", default=os.getenv("INTERFACE"), help="Modem interface name (e.g. wwan0, usb0)")
    parser.add_argument("--reg-secret", default=os.getenv("REGISTRATION_SECRET"), help="Optional REGISTRATION_SECRET for first-time provisioning")

    args = parser.parse_args()

    if not args.url:
        print("[ERROR] Registry URL must be specified via --url or REGISTRY_URL env var")
        sys.exit(1)
    if not args.token:
        print("[ERROR] Device token must be specified via --token or DEVICE_TOKEN env var")
        sys.exit(1)

    client = DroneRegistrationClient(
        registry_url=args.url,
        device_id=args.id,
        device_token=args.token,
        port=args.port,
        poll_interval=args.interval,
        preferred_iface=args.iface,
        registration_secret=args.reg_secret,
    )
    client.run_forever()


if __name__ == "__main__":
    main()

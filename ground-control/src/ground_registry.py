import os
import sys
import time
import socket
import logging
import threading
from typing import Optional, Dict, Any

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("GroundRegistry")

DEFAULT_REGISTRY_URL = "https://drone-registry.vercel.app"
DEFAULT_GROUND_ID = "GROUND-001"
DEFAULT_TOKEN = "acfd09978673d60a0be1121ee701805cdc275f14a1849e44"
DEFAULT_PORT = 5005


def detect_laptop_ipv6() -> Optional[str]:
    """
    Detects the active, routable global IPv6 address on this machine.
    Uses UDP route resolution against Google IPv6 DNS (0ms, 0 external packets sent).
    """
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        s.connect(("2001:4860:4860::8888", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and ip.startswith("2") and not ip.startswith("fe80"):
            return ip
    except Exception:
        pass

    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            cand = item[4][0]
            if cand and cand.startswith("2") and not cand.startswith("fe80"):
                return cand
    except Exception:
        pass

    return None


class GroundStationRegistryClient:
    """
    Manages continuous zero-touch discovery registration and heartbeat for the Ground Station (Laptop).
    Ensures the Android transmitter phone can always discover this laptop's dynamic IPv6 via Vercel.
    """

    def __init__(
        self,
        registry_url: str = DEFAULT_REGISTRY_URL,
        device_id: str = DEFAULT_GROUND_ID,
        device_token: str = DEFAULT_TOKEN,
        port: int = DEFAULT_PORT,
        heartbeat_interval: int = 20,
    ):
        self.registry_url = registry_url.rstrip("/")
        self.device_id = device_id.strip().upper()
        self.device_token = device_token
        self.port = port
        self.heartbeat_interval = heartbeat_interval

        self.current_ipv6: Optional[str] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def register(self, timeout: float = 6.0) -> bool:
        """Registers or re-registers the laptop's current IPv6 with Vercel."""
        if not requests:
            logger.warning("requests library missing; cannot register with Vercel")
            return False

        ip = detect_laptop_ipv6()
        if not ip:
            logger.warning("No global IPv6 detected on laptop network interfaces")
            return False

        url = f"{self.registry_url}/api/register"
        payload = {
            "deviceId": self.device_id,
            "ipv6": ip,
            "port": self.port,
            "token": self.device_token,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.device_token}",
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                self.current_ipv6 = ip
                logger.info(f"Successfully registered {self.device_id} on Vercel at [{ip}]:{self.port}")
                return True
            else:
                logger.warning(f"Registration failed HTTP {resp.status_code}: {resp.text}")
                return False
        except Exception as e:
            logger.warning(f"Registration network error: {e}")
            return False

    def heartbeat(self, timeout: float = 5.0) -> bool:
        """Sends a keep-alive pulse to maintain ONLINE status in Vercel registry."""
        if not requests:
            return False

        url = f"{self.registry_url}/api/heartbeat"
        payload = {
            "deviceId": self.device_id,
            "token": self.device_token,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.device_token}",
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
            return resp.status_code == 200
        except Exception:
            return False

    def lookup_drone(self, drone_id: str = "DRONE-001", timeout: float = 4.0) -> Optional[Dict[str, Any]]:
        """Queries Vercel to discover the current IPv6 and status of the drone transmitter."""
        if not requests:
            return None

        url = f"{self.registry_url}/api/lookup"
        try:
            resp = requests.get(url, params={"deviceId": drone_id}, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.debug(f"Drone lookup failed: {e}")
        return None

    def start_background_daemon(self):
        """Starts the background thread keeping the registration fresh every 20s."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._daemon_loop, daemon=True, name="GroundRegistryDaemon")
        self._thread.start()
        logger.info(f"GroundStation registry daemon started (syncing {self.device_id} to {self.registry_url})")

    def _daemon_loop(self):
        # 1. Initial registration
        self.register()

        while self._running:
            for _ in range(self.heartbeat_interval):
                if not self._running:
                    return
                time.sleep(1.0)

            # Check if laptop IP changed (e.g. Wi-Fi reconnected)
            fresh_ip = detect_laptop_ipv6()
            if fresh_ip and fresh_ip != self.current_ipv6:
                logger.info(f"Laptop IP changed: {self.current_ipv6} -> {fresh_ip}. Re-registering...")
                self.register()
            else:
                success = self.heartbeat()
                if not success:
                    # If heartbeat rejected (e.g., cold lambda reset), do full register
                    self.register()

    def stop(self):
        """Stops the background daemon."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("GroundStation registry daemon stopped")

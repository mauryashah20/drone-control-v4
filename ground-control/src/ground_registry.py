import os
import sys
import time
import socket
import logging
import threading
from typing import Optional, Dict, Any, Callable

try:
    import requests
except ImportError:
    requests = None

logger = logging.getLogger("GroundRegistry")

DEFAULT_REGISTRY_URL = "https://drone-registry-one.vercel.app"
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
        on_peer_update: Optional[Callable[[str, int], None]] = None,
    ):
        self.registry_url = registry_url.rstrip("/")
        self.device_id = device_id.strip().upper()
        self.device_token = device_token
        self.port = port
        self.heartbeat_interval = heartbeat_interval
        self.on_peer_update = on_peer_update

        self.current_ipv6: Optional[str] = None
        self.peer_target: Optional[Dict[str, Any]] = None
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_lookup_time: float = 0.0

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
                data = resp.json()
                logger.info(f"Successfully registered {self.device_id} on Vercel at [{ip}]:{self.port}")
                if "peerTarget" in data and data["peerTarget"]:
                    self.peer_target = data["peerTarget"]
                    p_ip = self.peer_target.get("ipv6")
                    p_port = self.peer_target.get("port", 5005)
                    logger.info(f"Vercel returned peer counterpart target: [{p_ip}]:{p_port}")
                    if self.on_peer_update and p_ip:
                        self.on_peer_update(p_ip, p_port)
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

    def lookup_if_unresponsive(
        self,
        drone_id: str = "DRONE-001",
        last_response_time: Optional[float] = None,
        unresponsive_threshold_s: float = 10.0,
    ) -> Optional[Dict[str, Any]]:
        """
        Rule: Only queries Vercel if the target drone has been unresponsive for > 10.0 seconds.
        Also enforces minimum 10.0s cooldown between lookups to prevent request quota exhaustion.
        """
        now = time.time()
        # If target has responded recently (< 10s), do not look up!
        if last_response_time is not None and last_response_time > 0 and (now - last_response_time < unresponsive_threshold_s):
            return None

        # Cooldown: At most one lookup every 10 seconds when unresponsive
        if now - self._last_lookup_time < unresponsive_threshold_s:
            return None

        self._last_lookup_time = now
        logger.info(f"Target '{drone_id}' unresponsive (> {unresponsive_threshold_s}s) — querying Vercel discovery...")
        return self.lookup_drone(drone_id)

    def start_background_daemon(self):
        """
        Rule 1: Registers once on initialization.
        Daemon thread only monitors local network interface and re-registers IF the local IP changes.
        Eliminates recurring heartbeats to avoid exhausting Vercel quota.
        """
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._daemon_loop, daemon=True, name="GroundRegistryDaemon")
        self._thread.start()
        logger.info(f"GroundStation registry daemon started (initial sync for {self.device_id})")

    def _daemon_loop(self):
        # 1. Initial registration on app startup
        try:
            self.register()
        except Exception as e:
            logger.warning(f"Initial ground station registration error: {e}")

        # 2. Initial lookup of latest counterpart app's IP (DRONE-001)
        if not self.peer_target:
            try:
                drone_data = self.lookup_drone("DRONE-001")
                if drone_data and drone_data.get("ipv6"):
                    p_ip = drone_data.get("ipv6")
                    p_port = drone_data.get("port", 5005)
                    logger.info(f"Initial startup lookup: Drone discovered at [{p_ip}]:{p_port}")
                    self.peer_target = drone_data
                    if self.on_peer_update and p_ip:
                        self.on_peer_update(p_ip, p_port)
            except Exception as e:
                logger.warning(f"Initial drone lookup error: {e}")

        consecutive_errors = 0
        while self._running:
            # Internal check every 750ms (0 external network requests, zero Vercel quota usage)
            if self._stop_event.wait(timeout=0.75):
                break

            if not self._running:
                break

            try:
                # Re-register ONLY if the laptop's own IP changed (e.g. Wi-Fi / Hotspot reconnected)
                fresh_ip = detect_laptop_ipv6()
                if fresh_ip and fresh_ip != self.current_ipv6:
                    logger.info(
                        f"[INTERNAL MONITOR] Laptop local IP changed: {self.current_ipv6} -> {fresh_ip}. "
                        f"Updating Vercel registry..."
                    )
                    self.register()
                elif self.current_ipv6 is None and fresh_ip:
                    # Retry registration if initial attempt occurred before network was up
                    self.register()
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors % 20 == 1:
                    logger.warning(f"Ground registry local monitor error ({consecutive_errors}): {e}")
                time.sleep(min(1.0, consecutive_errors * 0.1))

    def stop(self):
        """Stops the background daemon."""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("GroundStation registry daemon stopped")

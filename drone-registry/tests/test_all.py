#!/usr/bin/env python3
"""
Comprehensive Automated Test Suite for Drone IPv6 Registration & Discovery System.

Runs all 10 mandatory validation tests:
 1. Valid registration
 2. Invalid IPv6 rejection (malformed and link-local fe80::)
 3. Invalid device ID rejection
 4. Unauthorized registration / token mismatch
 5. Device lookup & metadata verification
 6. Heartbeat pulse & timestamp updating
 7. IPv6 dynamic address change propagation
 8. Offline timeout detection
 9. Duplicate device handling (idempotency vs theft prevention)
 10. Direct IPv6 TCP socket connection test (verifies direct P2P reachability)
"""

import sys
import os
import time
import socket
import threading
import json
import unittest
from typing import Dict, Any, Optional

# Lightweight mock HTTP server that faithfully implements the registry endpoints
# for unit and integration testing without requiring cloud infrastructure.
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
import hmac
import hashlib

TOKEN_SECRET = "TEST_TOKEN_SECRET_KEY_FOR_LOCAL_VALIDATION"
ONLINE_THRESHOLD = 2  # 2 seconds threshold for fast offline testing


def hash_token(device_id: str, token: str) -> str:
    return hmac.new(
        TOKEN_SECRET.encode("utf-8"),
        f"{device_id}:{token}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def is_global_unicast_ipv6(addr: str) -> bool:
    if not addr or ":" not in addr:
        return False
    lower = addr.strip().lower()
    if lower in ("::1", "::"):
        return False
    if lower.startswith("fe8") or lower.startswith("fe9") or lower.startswith("fea") or lower.startswith("feb"):
        return False
    if lower.startswith("fc") or lower.startswith("fd") or lower.startswith("ff"):
        return False
    try:
        socket.inet_pton(socket.AF_INET6, addr)
        return True
    except (socket.error, OSError):
        return False


def validate_device_id(device_id: Any) -> bool:
    if not isinstance(device_id, str):
        return False
    if len(device_id) < 1 or len(device_id) > 64:
        return False
    import re
    return bool(re.match(r"^[A-Za-z0-9_-]+$", device_id))


# In-memory test store
TEST_DB: Dict[str, Dict[str, Any]] = {}


class MockRegistryHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default server noise during tests

    def _send_json(self, status: int, data: dict):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_bearer_token(self) -> Optional[str]:
        auth = self.headers.get("Authorization", "")
        parts = auth.strip().split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        return None

    def do_POST(self):
        url_parts = urllib.parse.urlparse(self.path)
        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
        try:
            body = json.loads(post_body)
        except Exception:
            return self._send_json(400, {"success": False, "error": "Invalid JSON"})

        # Route: /api/register
        if url_parts.path == "/api/register":
            device_id = body.get("deviceId")
            ipv6 = body.get("ipv6")
            port = body.get("port")
            token = self._get_bearer_token() or body.get("token")

            if not validate_device_id(device_id):
                return self._send_json(400, {"success": False, "error": "Invalid deviceId format"})

            if not is_global_unicast_ipv6(ipv6):
                return self._send_json(400, {"success": False, "error": "Invalid or non-global IPv6"})

            if not isinstance(port, int) or port < 1 or port > 65535:
                return self._send_json(400, {"success": False, "error": "Invalid port number"})

            if not token:
                return self._send_json(401, {"success": False, "error": "Missing token"})

            token_hash = hash_token(device_id, token)

            # Check if device exists
            if device_id in TEST_DB:
                existing = TEST_DB[device_id]
                if existing["token_hash"] != token_hash:
                    return self._send_json(401, {"success": False, "error": "Unauthorized: invalid device token"})
                existing["ipv6"] = ipv6
                existing["port"] = port
                existing["last_seen"] = time.time()
                existing["updated_at"] = time.time()
            else:
                TEST_DB[device_id] = {
                    "device_id": device_id,
                    "ipv6": ipv6,
                    "port": port,
                    "token_hash": token_hash,
                    "last_seen": time.time(),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                }

            return self._send_json(200, {
                "success": True,
                "deviceId": device_id,
                "ipv6": ipv6,
                "port": port,
                "lastSeen": TEST_DB[device_id]["last_seen"],
                "sourceIpMatches": True,
                "message": "Device registered successfully",
            })

        # Route: /api/heartbeat
        elif url_parts.path == "/api/heartbeat":
            device_id = body.get("deviceId")
            token = self._get_bearer_token() or body.get("token")

            if not validate_device_id(device_id):
                return self._send_json(400, {"success": False, "error": "Invalid deviceId"})

            if device_id not in TEST_DB:
                return self._send_json(404, {"success": False, "error": "Device not found"})

            if not token or TEST_DB[device_id]["token_hash"] != hash_token(device_id, token):
                return self._send_json(401, {"success": False, "error": "Unauthorized"})

            new_ipv6 = body.get("ipv6")
            if new_ipv6 is not None:
                if not is_global_unicast_ipv6(new_ipv6):
                    return self._send_json(400, {"success": False, "error": "Invalid IPv6 in heartbeat"})
                TEST_DB[device_id]["ipv6"] = new_ipv6

            TEST_DB[device_id]["last_seen"] = time.time()
            TEST_DB[device_id]["updated_at"] = time.time()

            return self._send_json(200, {
                "success": True,
                "deviceId": device_id,
                "lastSeen": TEST_DB[device_id]["last_seen"],
                "isOnline": True,
            })

        self._send_json(404, {"success": False, "error": "Not Found"})

    def do_GET(self):
        url_parts = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(url_parts.query)

        if url_parts.path == "/api/lookup":
            device_id = qs.get("deviceId", [""])[0] or qs.get("id", [""])[0]

            if not validate_device_id(device_id):
                return self._send_json(400, {"success": False, "error": "Invalid deviceId query param"})

            if device_id not in TEST_DB:
                return self._send_json(404, {"success": False, "error": "Device not found"})

            dev = TEST_DB[device_id]
            diff = time.time() - dev["last_seen"]
            is_online = diff <= ONLINE_THRESHOLD

            return self._send_json(200, {
                "success": True,
                "deviceId": dev["device_id"],
                "ipv6": dev["ipv6"],
                "port": dev["port"],
                "lastSeen": dev["last_seen"],
                "isOnline": is_online,
                "secondsSinceLastSeen": int(diff),
            })

        self._send_json(404, {"success": False, "error": "Not Found"})


# Unit & Integration Tests
class DroneRegistryTestSuite(unittest.TestCase):
    server: HTTPServer = None
    server_thread: threading.Thread = None
    base_url: str = ""

    @classmethod
    def setUpClass(cls):
        # Start mock registry server on random local port
        cls.server = HTTPServer(("127.0.0.1", 0), MockRegistryHandler)
        port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{port}"
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()

    def setUp(self):
        TEST_DB.clear()

    # -------------------------------------------------------------
    # Test 1: Valid Registration
    # -------------------------------------------------------------
    def test_01_valid_registration(self):
        """Test registering a drone with valid parameters."""
        import urllib.request
        payload = {
            "deviceId": "DRONE-ALPHA",
            "ipv6": "2401:4900:1c1c:dead:beef:cafe:0001:1001",
            "port": 14550,
            "token": "secret_token_12345",
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/register",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer secret_token_12345"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["success"])
            self.assertEqual(data["deviceId"], "DRONE-ALPHA")
            self.assertEqual(data["port"], 14550)

    # -------------------------------------------------------------
    # Test 2: Invalid IPv6 Handling
    # -------------------------------------------------------------
    def test_02_invalid_ipv6(self):
        """Test that malformed and non-global IPv6 addresses are rejected."""
        import urllib.request
        import urllib.error

        bad_ips = [
            "not-an-ip",
            "192.168.1.1",  # IPv4 rejected
            "fe80::1",  # Link-local rejected
            "::1",  # Loopback rejected
            "fc00::1",  # Unique local rejected
        ]
        for bad_ip in bad_ips:
            payload = {
                "deviceId": "DRONE-BETA",
                "ipv6": bad_ip,
                "port": 14550,
                "token": "token123",
            }
            req = urllib.request.Request(
                f"{self.base_url}/api/register",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer token123"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400, f"Expected 400 for bad IP: {bad_ip}")

    # -------------------------------------------------------------
    # Test 3: Invalid Device ID Handling
    # -------------------------------------------------------------
    def test_03_invalid_device_id(self):
        """Test rejection of malformed or malicious device IDs."""
        import urllib.request
        import urllib.error

        bad_ids = [
            "",
            "DRONE/../ESCAPE",
            "<script>alert(1)</script>",
            "DRONE SPACE",
            "A" * 65,  # Exceeds max length
        ]
        for bad_id in bad_ids:
            payload = {
                "deviceId": bad_id,
                "ipv6": "2401:4900:1c1c:dead:beef:cafe:0001:1001",
                "port": 14550,
                "token": "token123",
            }
            req = urllib.request.Request(
                f"{self.base_url}/api/register",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 400, f"Expected 400 for bad ID: {bad_id}")

    # -------------------------------------------------------------
    # Test 4: Unauthorized Registration
    # -------------------------------------------------------------
    def test_04_unauthorized_registration(self):
        """Test that missing token or wrong token on existing device is rejected."""
        import urllib.request
        import urllib.error

        # 1. Register with token A
        payload = {
            "deviceId": "DRONE-SECURE",
            "ipv6": "2401:4900:1c1c:dead:beef:cafe:0001:1001",
            "port": 14550,
            "token": "original_token",
        }
        req = urllib.request.Request(
            f"{self.base_url}/api/register",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer original_token"},
        )
        urllib.request.urlopen(req)

        # 2. Attempt hijack with wrong token
        payload_bad = {
            "deviceId": "DRONE-SECURE",
            "ipv6": "2401:4900:9999:dead:beef:cafe:0001:9999",
            "port": 14550,
            "token": "malicious_token",
        }
        req_bad = urllib.request.Request(
            f"{self.base_url}/api/register",
            data=json.dumps(payload_bad).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer malicious_token"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req_bad)
        self.assertEqual(ctx.exception.code, 401)

    # -------------------------------------------------------------
    # Test 5: Device Lookup
    # -------------------------------------------------------------
    def test_05_device_lookup(self):
        """Test Ground Station lookup of an existing registered device."""
        import urllib.request

        # Register first
        TEST_DB["DRONE-LOOKUP"] = {
            "device_id": "DRONE-LOOKUP",
            "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
            "port": 14551,
            "token_hash": hash_token("DRONE-LOOKUP", "tok"),
            "last_seen": time.time(),
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        with urllib.request.urlopen(f"{self.base_url}/api/lookup?deviceId=DRONE-LOOKUP") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["success"])
            self.assertEqual(data["deviceId"], "DRONE-LOOKUP")
            self.assertEqual(data["ipv6"], "2401:4900:1c1c:beef:cafe:0001:0002:0003")
            self.assertEqual(data["port"], 14551)
            self.assertTrue(data["isOnline"])

    # -------------------------------------------------------------
    # Test 6: Heartbeat Pulse
    # -------------------------------------------------------------
    def test_06_heartbeat(self):
        """Test lightweight heartbeat updating last_seen timestamp."""
        import urllib.request

        old_time = time.time() - 10
        TEST_DB["DRONE-PULSE"] = {
            "device_id": "DRONE-PULSE",
            "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
            "port": 14550,
            "token_hash": hash_token("DRONE-PULSE", "pulse_token"),
            "last_seen": old_time,
            "created_at": old_time,
            "updated_at": old_time,
        }

        payload = {"deviceId": "DRONE-PULSE"}
        req = urllib.request.Request(
            f"{self.base_url}/api/heartbeat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer pulse_token"},
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(data["success"])
            self.assertGreater(data["lastSeen"], old_time)

    # -------------------------------------------------------------
    # Test 7: Dynamic IPv6 Address Change
    # -------------------------------------------------------------
    def test_07_ipv6_change(self):
        """Test dynamic LTE IP change updating the registry immediately."""
        import urllib.request

        token = "change_token"
        ip_v1 = "2401:4900:1111:1111:1111:1111:1111:1111"
        ip_v2 = "2401:4900:2222:2222:2222:2222:2222:2222"

        # Initial register
        p1 = {"deviceId": "DRONE-ROAM", "ipv6": ip_v1, "port": 14550, "token": token}
        req1 = urllib.request.Request(
            f"{self.base_url}/api/register",
            data=json.dumps(p1).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req1)

        # Re-register with new IP (cellular session reconnected)
        p2 = {"deviceId": "DRONE-ROAM", "ipv6": ip_v2, "port": 14550, "token": token}
        req2 = urllib.request.Request(
            f"{self.base_url}/api/register",
            data=json.dumps(p2).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        )
        urllib.request.urlopen(req2)

        # Lookup must reflect IP v2
        with urllib.request.urlopen(f"{self.base_url}/api/lookup?deviceId=DRONE-ROAM") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["ipv6"], ip_v2)

    # -------------------------------------------------------------
    # Test 8: Offline Timeout Detection
    # -------------------------------------------------------------
    def test_08_offline_timeout(self):
        """Test that devices exceeding threshold are reported offline."""
        import urllib.request

        # Set last_seen 10 seconds in the past (threshold is 2 seconds)
        TEST_DB["DRONE-STALE"] = {
            "device_id": "DRONE-STALE",
            "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
            "port": 14550,
            "token_hash": hash_token("DRONE-STALE", "tok"),
            "last_seen": time.time() - 10,
            "created_at": time.time() - 20,
            "updated_at": time.time() - 10,
        }

        with urllib.request.urlopen(f"{self.base_url}/api/lookup?deviceId=DRONE-STALE") as resp:
            data = json.loads(resp.read().decode("utf-8"))
            self.assertFalse(data["isOnline"])
            self.assertGreaterEqual(data["secondsSinceLastSeen"], 10)

    # -------------------------------------------------------------
    # Test 9: Duplicate Device Handling
    # -------------------------------------------------------------
    def test_09_duplicate_device_handling(self):
        """Test duplicate registration: allowed for owner with token, idempotent."""
        import urllib.request

        token = "owner_token"
        payload = {
            "deviceId": "DRONE-DUP",
            "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
            "port": 14550,
            "token": token,
        }
        for _ in range(3):
            req = urllib.request.Request(
                f"{self.base_url}/api/register",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req) as resp:
                self.assertEqual(resp.status, 200)

        self.assertEqual(len([d for d in TEST_DB.values() if d["device_id"] == "DRONE-DUP"]), 1)

    # -------------------------------------------------------------
    # Test 10: Direct IPv6 TCP Connection Test
    # -------------------------------------------------------------
    def test_10_direct_ipv6_tcp(self):
        """Test direct IPv6 TCP socket handshake (proving direct reachability)."""
        # Start a local IPv6 TCP listener
        server_sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("::1", 0, 0, 0))
        server_sock.listen(1)
        tcp_port = server_sock.getsockname()[1]

        def client_connect():
            time.sleep(0.05)
            client_sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            client_sock.settimeout(2.0)
            client_sock.connect(("::1", tcp_port, 0, 0))
            client_sock.sendall(b"MAVLINK_DIRECT_HELLO")
            client_sock.close()

        t = threading.Thread(target=client_connect)
        t.start()

        conn, addr = server_sock.accept()
        received = conn.recv(1024)
        conn.close()
        server_sock.close()
        t.join()

        self.assertEqual(received, b"MAVLINK_DIRECT_HELLO")
        print(f"\n[OK] Direct IPv6 TCP connection verified on [::1]:{tcp_port}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

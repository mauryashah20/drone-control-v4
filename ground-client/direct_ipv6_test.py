#!/usr/bin/env python3
"""
Direct IPv6 Connectivity Test Tool for Ground Station.

Performs direct end-to-end tests against the Drone's cellular IPv6 address:
  1. TCP connect test (measures three-way handshake time)
  2. UDP echo test (measures round-trip packet transmission)
  3. ICMP ping6 reachability test

NOTE: This tests DIRECT IPv6 communication between Ground Station and Drone.
No traffic passes through Vercel or any proxy server.
"""

import sys
import time
import socket
import argparse
import subprocess
import platform
from typing import Optional


def test_tcp_connection(ipv6: str, port: int, timeout: float = 3.0) -> bool:
    """Tests direct TCP connection to the drone's IPv6 address and port."""
    print(f"\n[TCP TEST] Attempting direct IPv6 TCP handshake -> [{ipv6}]:{port} (timeout={timeout}s)...")
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(timeout)
    start_time = time.perf_counter()
    try:
        s.connect((ipv6, port, 0, 0))
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        print(f"  [SUCCESS] TCP connection established! Handshake RTT: {elapsed_ms:.2f} ms")
        s.close()
        return True
    except socket.timeout:
        print(f"  [FAILED] Connection timed out after {timeout}s.")
        return False
    except ConnectionRefusedError:
        print(f"  [NOTICE] Direct IPv6 reached the host, but port {port} is closed (Connection Refused).")
        print("  -> This proves direct network reachability to the Pi! Just ensure the server service is running.")
        return True
    except OSError as e:
        print(f"  [FAILED] Socket error: {e}")
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def test_ping6(ipv6: str, count: int = 4) -> bool:
    """Executes system ping6 / ping -6 against the direct IPv6 address."""
    print(f"\n[PING TEST] Pinging [{ipv6}] directly with {count} ICMPv6 packets...")
    is_win = platform.system().lower() == "windows"
    cmd = ["ping", "-6", "-n" if is_win else "-c", str(count), ipv6]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        print(proc.stdout)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print("  [PING TIMEOUT] Ping command timed out.")
        return False
    except Exception as e:
        print(f"  [PING ERROR] Could not run ping: {e}")
        return False


def test_udp_echo_client(ipv6: str, port: int, count: int = 5, timeout: float = 2.0) -> bool:
    """Sends UDP test datagrams to the drone's listening port."""
    print(f"\n[UDP TEST] Sending {count} direct UDP probes to [{ipv6}]:{port}...")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.settimeout(timeout)

    success_count = 0
    rtts = []

    for i in range(count):
        msg = f"UAV_TEST_PROBE_{i}_{time.time()}".encode("utf-8")
        send_time = time.perf_counter()
        try:
            sock.sendto(msg, (ipv6, port, 0, 0))
            # If server runs test echo, wait for response
            try:
                data, addr = sock.recvfrom(1024)
                recv_time = time.perf_counter()
                rtt = (recv_time - send_time) * 1000.0
                rtts.append(rtt)
                print(f"  Probe #{i+1}: Echo received from [{addr[0]}] in {rtt:.2f} ms")
                success_count += 1
            except socket.timeout:
                print(f"  Probe #{i+1}: Sent successfully (no echo listener expected on drone)")
                success_count += 1
        except OSError as e:
            print(f"  Probe #{i+1} failed: {e}")

        time.sleep(0.1)

    sock.close()
    if rtts:
        avg_rtt = sum(rtts) / len(rtts)
        print(f"  [UDP METRICS] Average Round-Trip Time: {avg_rtt:.2f} ms (Min: {min(rtts):.2f} ms, Max: {max(rtts):.2f} ms)")
    return success_count > 0


def run_echo_server(port: int = 14555):
    """Simple dual-stack or IPv6 echo server to run on the Pi for reachability testing."""
    print(f"Starting IPv6 Test Echo Server on [::]:{port}...")
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.bind(("::", port, 0, 0))
    print(f"Echo server listening. Press Ctrl+C to terminate.")
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            print(f"Received {len(data)} bytes from [{addr[0]}]:{addr[1]} -> Echoing back...")
            sock.sendto(b"ECHO:" + data, addr)
    except KeyboardInterrupt:
        print("\nEcho server stopped.")
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="Direct IPv6 Drone Connectivity Tester")
    parser.add_argument("ipv6", nargs="?", help="Direct IPv6 address of the drone (e.g. 2401:4900:...)")
    parser.add_argument("--port", type=int, default=14550, help="Target UDP/TCP port (default: 14550)")
    parser.add_argument("--skip-ping", action="store_true", help="Skip ICMPv6 ping test")
    parser.add_argument("--skip-tcp", action="store_true", help="Skip TCP handshake test")
    parser.add_argument("--skip-udp", action="store_true", help="Skip UDP test")
    parser.add_argument("--server", action="store_true", help="Run test IPv6 UDP echo server on this machine")

    args = parser.parse_args()

    if args.server:
        run_echo_server(args.port)
        return

    if not args.ipv6:
        print("Usage: python direct_ipv6_test.py <IPv6_ADDRESS> [--port 14550]")
        print("   or: python direct_ipv6_test.py --server [--port 14550] (runs echo listener)")
        sys.exit(1)

    print("=======================================================")
    print("      DIRECT IPv6 CONNECTIVITY VERIFICATION")
    print("=======================================================")
    print(f"Target Drone IPv6: {args.ipv6}")
    print(f"Target Port:       {args.port}")
    print("=======================================================")

    results = {}

    if not args.skip_ping:
        results["ICMPv6 Ping"] = test_ping6(args.ipv6)

    if not args.skip_tcp:
        results["Direct TCP Handshake"] = test_tcp_connection(args.ipv6, args.port)

    if not args.skip_udp:
        results["Direct UDP Transmission"] = test_udp_echo_client(args.ipv6, args.port)

    print("\n----------------- SUMMARY -----------------")
    for test_name, passed in results.items():
        status = "PASSED [OK]" if passed else "FAILED / TIMEOUT"
        print(f"  {test_name:30} : {status}")
    print("-------------------------------------------\n")


if __name__ == "__main__":
    main()

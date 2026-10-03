"""
bt_bridge.py — ESP32 Bluetooth-Serial → UDP bridge (Windows)
=============================================================
Reads MAVLink bytes from the ESP32 "APM-Bridge" Bluetooth COM port and
forwards them to UDP 127.0.0.1:14551 so the ground-control telemetry
router picks them up exactly as before.

Also forwards any UDP downlink (GCS commands from Mission Planner) back
to the ESP32 over Bluetooth Serial.

Usage:
    python bt_bridge.py --port COM5 --baud 57600

COM port:
    Windows assigns a virtual COM port when you pair "APM-Bridge".
    Check Device Manager → Ports (COM & LPT) after pairing.
    Or run:  python -m serial.tools.list_ports
"""

import argparse
import socket
import threading
import time
import sys

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    sys.exit("[ERROR] pyserial not installed. Run:  pip install pyserial")


# ──────────────────────────────────────────────────────────────────────────────
#  Default config — matches ground-control telemetry router exactly
# ──────────────────────────────────────────────────────────────────────────────
DEFAULT_BT_BAUD      = 57600
UDP_TELEM_PORT       = 14551   # ground-control listens here for MAVLink uplink
UDP_GCS_RETURN_PORT  = 14552   # ground-control sends downlink here
UDP_HOST             = "127.0.0.1"
RECONNECT_DELAY_S    = 3.0
READ_TIMEOUT_S       = 1.0


def list_com_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("  (no COM ports found)")
    for p in ports:
        print(f"  {p.device:10s} — {p.description}")


def find_bt_port():
    """Auto-detect the first COM port whose description mentions Bluetooth."""
    for p in serial.tools.list_ports.comports():
        if "bluetooth" in p.description.lower() or "bt" in p.description.lower():
            return p.device
    return None


# ──────────────────────────────────────────────────────────────────────────────
class BtBridge:
    def __init__(self, port: str, baud: int):
        self.port = port
        self.baud = baud
        self._ser: serial.Serial | None = None
        self._running = threading.Event()

        # UDP socket — bidirectional
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((UDP_HOST, UDP_GCS_RETURN_PORT))
        self._udp.settimeout(0.05)

        # Stats
        self.apm_rx = 0
        self.bt_tx  = 0
        self.udp_rx = 0
        self.udp_tx = 0

    def _open_serial(self) -> bool:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baud,
                timeout=READ_TIMEOUT_S,
            )
            print(f"[BT-BRIDGE] Opened {self.port} @ {self.baud} baud")
            return True
        except serial.SerialException as e:
            print(f"[BT-BRIDGE] Cannot open {self.port}: {e}")
            return False

    def _bt_to_udp_thread(self):
        """Reads MAVLink bytes from BT Serial → forwards to UDP 14551."""
        while self._running.is_set():
            if not self._ser or not self._ser.is_open:
                time.sleep(RECONNECT_DELAY_S)
                continue
            try:
                data = self._ser.read(512)
                if data:
                    self.apm_rx += len(data)
                    self._udp.sendto(data, (UDP_HOST, UDP_TELEM_PORT))
                    self.udp_tx += 1
            except serial.SerialException as e:
                print(f"[BT-BRIDGE] Serial error: {e} — reconnecting in {RECONNECT_DELAY_S}s")
                try:
                    self._ser.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_DELAY_S)
                self._open_serial()
            except Exception:
                pass

    def _udp_to_bt_thread(self):
        """Reads GCS downlink from UDP → forwards to BT Serial → APM."""
        while self._running.is_set():
            try:
                data, _ = self._udp.recvfrom(512)
                if data and self._ser and self._ser.is_open:
                    self._ser.write(data)
                    self.bt_tx  += len(data)
                    self.udp_rx += 1
            except socket.timeout:
                continue
            except Exception:
                pass

    def _status_thread(self):
        """Prints running stats every 5 seconds."""
        while self._running.is_set():
            time.sleep(5)
            ser_ok = self._ser and self._ser.is_open
            print(
                f"[BT-BRIDGE]  BT={self.port} {'OPEN' if ser_ok else 'CLOSED'}"
                f"  APM_RX={self.apm_rx}B  UDP_TX={self.udp_tx}pkt"
                f"  GCS_RX={self.udp_rx}pkt  BT_TX={self.bt_tx}B"
            )

    def start(self):
        if not self._open_serial():
            print("[BT-BRIDGE] Could not open port. Bridge will retry automatically.")

        self._running.set()
        threading.Thread(target=self._bt_to_udp_thread, daemon=True, name="BT→UDP").start()
        threading.Thread(target=self._udp_to_bt_thread, daemon=True, name="UDP→BT").start()
        threading.Thread(target=self._status_thread,    daemon=True, name="Status").start()
        print(f"[BT-BRIDGE] Running.  BT→UDP:{UDP_TELEM_PORT}  UDP:{UDP_GCS_RETURN_PORT}→BT")
        print("[BT-BRIDGE] Press Ctrl+C to stop.")

    def stop(self):
        self._running.clear()
        try:
            self._udp.close()
        except Exception:
            pass
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        print("[BT-BRIDGE] Stopped.")


# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ESP32 Bluetooth-Serial → UDP bridge for APM MAVLink telemetry"
    )
    parser.add_argument("--port", default=None,
                        help="Bluetooth COM port (e.g. COM5). Auto-detects if omitted.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BT_BAUD,
                        help=f"Baud rate (default: {DEFAULT_BT_BAUD})")
    parser.add_argument("--list", action="store_true",
                        help="List available COM ports and exit")
    args = parser.parse_args()

    if args.list:
        print("Available COM ports:")
        list_com_ports()
        return

    port = args.port
    if not port:
        port = find_bt_port()
        if not port:
            print("[ERROR] Could not auto-detect Bluetooth COM port.")
            print("  Pair the ESP32 first, then run with --port COMx")
            print("\nAvailable ports:")
            list_com_ports()
            sys.exit(1)
        print(f"[BT-BRIDGE] Auto-detected BT port: {port}")

    bridge = BtBridge(port=port, baud=args.baud)
    bridge.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        bridge.stop()


if __name__ == "__main__":
    main()

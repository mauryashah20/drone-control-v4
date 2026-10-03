import os
import math
import socket
import select
import threading
import time
from dataclasses import dataclass
from typing import Optional, Callable, Dict, Tuple

# Enable MAVLink 2.0 in pymavlink
os.environ["MAVLINK20"] = "1"
from pymavlink.dialects.v20 import common as mavlink2
from pymavlink import mavutil


@dataclass
class DroneTelemetryState:
    connected: bool = False
    last_heartbeat_time: float = 0.0
    armed: bool = False
    flight_mode: str = "WAITING"
    battery_voltage: float = 0.0
    battery_current: float = 0.0
    battery_remaining_pct: int = -1
    gps_fix_type: int = 0  # 0: No GPS, 1: No Fix, 2: 2D, 3: 3D, 4: DGPS, 5: RTK Float, 6: RTK Fixed
    satellites_visible: int = 0
    altitude_relative_m: float = 0.0
    altitude_msl_m: float = 0.0
    heading_deg: float = 0.0
    groundspeed_mps: float = 0.0
    climb_rate_mps: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    yaw_deg: float = 0.0
    sys_id: int = 1
    comp_id: int = 1
    uplink_packets: int = 0
    downlink_packets: int = 0

    @property
    def is_heartbeat_fresh(self) -> bool:
        return self.connected and (time.time() - self.last_heartbeat_time < 3.5)

    @property
    def gps_fix_str(self) -> str:
        fixes = {
            0: "NO GPS",
            1: "NO FIX",
            2: "2D FIX",
            3: "3D FIX",
            4: "DGPS",
            5: "RTK FLT",
            6: "RTK FIX",
        }
        return fixes.get(self.gps_fix_type, f"GPS {self.gps_fix_type}")


class TelemetryRouter:
    """
    High-performance bidirectional MAVLink gateway:
    1. Receives MAVLink UDP packets from Phone Transmitter (ZeroTier port, default 14551).
    2. Transparently forwards to Mission Planner (localhost UDP 14550 / TCP 5760).
    3. Receives GCS commands/heartbeats from Mission Planner and routes back to Phone.
    4. Non-destructively parses live flight telemetry to feed the FPV HUD.
    """

    def __init__(
        self,
        phone_bind_ip: str = "::",
        phone_bind_port: int = 14551,
        mp_host: str = "127.0.0.1",
        mp_port: int = 14550,
        mp_local_port: int = 14552,
        tcp_port: int = 5760,
        enable_tcp: bool = True,
        on_telemetry_update: Optional[Callable[[DroneTelemetryState], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ):
        self.phone_bind_ip = phone_bind_ip
        self.phone_bind_port = phone_bind_port
        self.mp_host = mp_host
        self.mp_port = mp_port
        self.mp_local_port = mp_local_port
        self.tcp_port = tcp_port
        self.enable_tcp = enable_tcp

        self.on_telemetry_update = on_telemetry_update
        self.on_log = on_log

        self.state = DroneTelemetryState()
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Network handles
        self._phone_sock: Optional[socket.socket] = None
        self._mp_udp_sock: Optional[socket.socket] = None
        self._tcp_server: Optional[socket.socket] = None
        self._tcp_clients: list[socket.socket] = []

        # Phone address tracking
        self._last_phone_addr: Optional[Tuple[str, int]] = None

        # MAVLink parser
        self._mav = mavlink2.MAVLink(None)
        self._mode_map: Dict[int, str] = mavutil.mode_mapping_bynumber(mavutil.mavlink.MAV_TYPE_QUADROTOR) or {}

    def _log(self, msg: str):
        if self.on_log:
            self.on_log(msg)
        else:
            print(f"[TELEM ROUTER] {msg}")

    def start(self):
        if self._running.is_set():
            return
        self._running.set()
        self._thread = threading.Thread(target=self._run_loop, name="TelemetryRouterThread", daemon=True)
        self._thread.start()
        self._log(f"Started routing: Phone listening on UDP {self.phone_bind_port} -> Mission Planner on {self.mp_host}:{self.mp_port}")

    def stop(self):
        if not self._running.is_set():
            return
        self._running.clear()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

        # Close sockets
        for s in [self._phone_sock, self._mp_udp_sock, self._tcp_server] + self._tcp_clients:
            if s:
                try:
                    s.close()
                except Exception:
                    pass
        self._phone_sock = None
        self._mp_udp_sock = None
        self._tcp_server = None
        self._tcp_clients.clear()
        self._log("Stopped telemetry router")

    def _run_loop(self):
        # 1. Setup UDP socket to listen for packets from Phone Transmitter (Dual-stack IPv6 + IPv4)
        try:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_addr = self.phone_bind_ip if self.phone_bind_ip not in ("0.0.0.0", "") else "::"
            sock.bind((bind_addr, self.phone_bind_port))
            sock.setblocking(False)
            self._phone_sock = sock
        except Exception:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                bind_addr = self.phone_bind_ip if self.phone_bind_ip != "::" else "0.0.0.0"
                sock.bind((bind_addr, self.phone_bind_port))
                sock.setblocking(False)
                self._phone_sock = sock
            except Exception as e:
                self._log(f"Error binding phone UDP socket ({self.phone_bind_ip}:{self.phone_bind_port}): {e}")
                return

        # 2. Setup UDP socket to communicate with Mission Planner (localhost)
        try:
            self._mp_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._mp_udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._mp_udp_sock.bind(("127.0.0.1", self.mp_local_port))
            self._mp_udp_sock.setblocking(False)
        except Exception as e:
            self._log(f"Warning: binding MP local socket failed, falling back to ephemeral: {e}")
            self._mp_udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._mp_udp_sock.setblocking(False)

        # 3. Setup optional TCP server for TCP-based GCS connection
        if self.enable_tcp:
            try:
                self._tcp_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._tcp_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._tcp_server.bind(("0.0.0.0", self.tcp_port))
                self._tcp_server.listen(2)
                self._tcp_server.setblocking(False)
                self._log(f"Mission Planner TCP bridge listening on 127.0.0.1:{self.tcp_port}")
            except Exception as e:
                self._log(f"TCP server setup skipped: {e}")
                self._tcp_server = None

        mp_dest_addr = (self.mp_host, self.mp_port)

        while self._running.is_set():
            try:
                read_list = [self._phone_sock, self._mp_udp_sock]
                if self._tcp_server:
                    read_list.append(self._tcp_server)
                read_list.extend(self._tcp_clients)

                readable, _, _ = select.select(read_list, [], [], 0.05)

                for sock in readable:
                    if sock is self._phone_sock:
                        # Uplink from Phone -> forward to Mission Planner
                        try:
                            data, addr = sock.recvfrom(4096)
                            if data:
                                self._last_phone_addr = addr
                                with self._lock:
                                    self.state.uplink_packets += 1

                                # Forward to Mission Planner UDP
                                try:
                                    self._mp_udp_sock.sendto(data, mp_dest_addr)
                                except Exception:
                                    pass

                                # Forward to connected TCP GCS clients
                                dead_clients = []
                                for client in self._tcp_clients:
                                    try:
                                        client.sendall(data)
                                    except Exception:
                                        dead_clients.append(client)
                                for dc in dead_clients:
                                    self._tcp_clients.remove(dc)
                                    try:
                                        dc.close()
                                    except Exception:
                                        pass

                                # Sniff and update telemetry state
                                self._parse_mavlink_bytes(data)

                        except (BlockingIOError, socket.error):
                            pass

                    elif sock is self._mp_udp_sock:
                        # Downlink from Mission Planner UDP -> forward to Phone
                        try:
                            data, _ = sock.recvfrom(4096)
                            if data and self._last_phone_addr:
                                self._phone_sock.sendto(data, self._last_phone_addr)
                                with self._lock:
                                    self.state.downlink_packets += 1
                        except (BlockingIOError, socket.error):
                            pass

                    elif sock is self._tcp_server:
                        # New TCP client connection (Mission Planner TCP)
                        try:
                            client_sock, client_addr = self._tcp_server.accept()
                            client_sock.setblocking(False)
                            self._tcp_clients.append(client_sock)
                            self._log(f"Mission Planner connected via TCP from {client_addr}")
                        except Exception:
                            pass

                    elif sock in self._tcp_clients:
                        # Downlink from TCP client -> forward to Phone
                        try:
                            data = sock.recv(4096)
                            if data and self._last_phone_addr:
                                self._phone_sock.sendto(data, self._last_phone_addr)
                                with self._lock:
                                    self.state.downlink_packets += 1
                            elif not data:
                                # Client disconnected
                                self._tcp_clients.remove(sock)
                                try:
                                    sock.close()
                                except Exception:
                                    pass
                        except (BlockingIOError, socket.error):
                            pass

            except Exception as e:
                if self._running.is_set():
                    self._log(f"Router cycle error: {e}")
                    time.sleep(0.01)

    def _parse_mavlink_bytes(self, data: bytes):
        try:
            msgs = self._mav.parse_buffer(data)
            if not msgs:
                return

            with self._lock:
                for msg in msgs:
                    msg_type = msg.get_type()

                    if msg_type == "HEARTBEAT":
                        self.state.connected = True
                        self.state.last_heartbeat_time = time.time()
                        self.state.sys_id = msg.get_srcSystem()
                        self.state.comp_id = msg.get_srcComponent()
                        self.state.armed = bool(msg.base_mode & mavlink2.MAV_MODE_FLAG_SAFETY_ARMED)

                        # Update mode mapping if vehicle type differs
                        veh_type = msg.type
                        if veh_type in [mavutil.mavlink.MAV_TYPE_FIXED_WING]:
                            mode_map = mavutil.mode_mapping_bynumber(mavutil.mavlink.MAV_TYPE_FIXED_WING) or {}
                        else:
                            mode_map = self._mode_map

                        self.state.flight_mode = mode_map.get(msg.custom_mode, f"MODE_{msg.custom_mode}")

                    elif msg_type == "SYS_STATUS":
                        if msg.voltage_battery != 65535:
                            self.state.battery_voltage = msg.voltage_battery / 1000.0
                        if msg.current_battery != -1:
                            self.state.battery_current = msg.current_battery / 100.0
                        self.state.battery_remaining_pct = msg.battery_remaining

                    elif msg_type == "BATTERY_STATUS":
                        voltages = [v / 1000.0 for v in msg.voltages if v != 65535]
                        if voltages:
                            self.state.battery_voltage = voltages[0]
                        self.state.battery_remaining_pct = msg.battery_remaining

                    elif msg_type == "GPS_RAW_INT":
                        self.state.gps_fix_type = msg.fix_type
                        self.state.satellites_visible = msg.satellites_visible

                    elif msg_type == "VFR_HUD":
                        self.state.groundspeed_mps = msg.groundspeed
                        self.state.heading_deg = msg.heading
                        self.state.altitude_msl_m = msg.alt
                        self.state.climb_rate_mps = msg.climb

                    elif msg_type == "GLOBAL_POSITION_INT":
                        self.state.altitude_relative_m = msg.relative_alt / 1000.0
                        self.state.altitude_msl_m = msg.alt / 1000.0

                    elif msg_type == "ATTITUDE":
                        self.state.pitch_deg = math.degrees(msg.pitch)
                        self.state.roll_deg = math.degrees(msg.roll)
                        self.state.yaw_deg = math.degrees(msg.yaw)

            if self.on_telemetry_update:
                self.on_telemetry_update(self.state)

        except Exception:
            pass

    def get_state(self) -> DroneTelemetryState:
        with self._lock:
            # Return a shallow copy
            return DroneTelemetryState(
                connected=self.state.connected,
                last_heartbeat_time=self.state.last_heartbeat_time,
                armed=self.state.armed,
                flight_mode=self.state.flight_mode,
                battery_voltage=self.state.battery_voltage,
                battery_current=self.state.battery_current,
                battery_remaining_pct=self.state.battery_remaining_pct,
                gps_fix_type=self.state.gps_fix_type,
                satellites_visible=self.state.satellites_visible,
                altitude_relative_m=self.state.altitude_relative_m,
                altitude_msl_m=self.state.altitude_msl_m,
                heading_deg=self.state.heading_deg,
                groundspeed_mps=self.state.groundspeed_mps,
                climb_rate_mps=self.state.climb_rate_mps,
                pitch_deg=self.state.pitch_deg,
                roll_deg=self.state.roll_deg,
                yaw_deg=self.state.yaw_deg,
                sys_id=self.state.sys_id,
                comp_id=self.state.comp_id,
                uplink_packets=self.state.uplink_packets,
                downlink_packets=self.state.downlink_packets,
            )

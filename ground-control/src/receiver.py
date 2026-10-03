import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Dict
import av
import numpy as np

HEADER_SIZE = 16  # 4 bytes frame_seq, 2 bytes chunk_idx, 2 bytes total_chunks, 8 bytes timestamp_ms
START_CODE = b"\x00\x00\x00\x01"

# Control packet signatures
CTRL_MAGIC = 0xFF
PACKET_KEEP_ALIVE = b"\xFF\xAA\x55\x00"
PACKET_HELLO = b"\xFF\x01\xCA\xFE"
PACKET_GOODBYE = b"\xFF\xDE\xAD\x01"
PACKET_APP_CLOSE = b"\xFF\xDE\xAD\x02"


class ConnectionState:
    SEARCHING = "SEARCHING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTING = "RECONNECTING"


@dataclass
class FrameStats:
    sequence: int
    sender_timestamp_ms: int
    transit_latency_ms: float
    decode_time_ms: float
    fps: float
    bitrate_kbps: float
    width: int
    height: int
    total_frames: int
    dropped_frames: int


class ZeroLatencyVideoReceiver:
    """
    Ultra-low latency H.264 video receiver over UDP (ZeroTier).
    Performs deterministic chunk assembly, automatic connect/disconnect detection,
    and zero-delay decoding with seamless auto-reconnect capability.
    """

    def __init__(
        self,
        bind_ip: str = "::",
        port: int = 5005,
        on_frame: Optional[Callable[[np.ndarray, FrameStats], None]] = None,
        on_connection_change: Optional[Callable[[bool, str, Optional[str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.bind_ip = bind_ip
        self.port = port
        self.on_frame = on_frame
        self.on_connection_change = on_connection_change
        self.on_log = on_log

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._lock = threading.Lock()

        # Connection status tracking
        self.is_connected = False
        self.connection_state = ConnectionState.SEARCHING
        self.disconnect_reason = "Waiting for initial connection..."
        self.last_packet_time: float = 0.0
        self.last_connected_time: float = 0.0
        self._last_sender_addr: Optional[tuple] = None

        # Decoder & Frame assembly
        self._decoder = self._create_decoder()
        self._frame_parts: Dict[int, Dict[int, bytes]] = {}
        self._frame_totals: Dict[int, int] = {}
        self._frame_timestamps: Dict[int, int] = {}
        self._last_completed_seq: Optional[int] = None
        self._dropped_frames = 0

        # Stats tracking
        self._total_frames = 0
        self._frame_times: list[float] = []
        self._bytes_received = 0
        self._last_bitrate_time = time.time()
        self._current_bitrate_kbps = 0.0
        self._last_stats = FrameStats(
            sequence=0,
            sender_timestamp_ms=0,
            transit_latency_ms=0.0,
            decode_time_ms=0.0,
            fps=0.0,
            bitrate_kbps=0.0,
            width=640,
            height=480,
            total_frames=0,
            dropped_frames=0,
        )

    def _create_decoder(self) -> av.CodecContext:
        """Configures a clean zero-latency PyAV decoder with 0 lookahead delay."""
        decoder = av.CodecContext.create("h264", "r")
        decoder.flags = av.codec.context.Flags.LOW_DELAY
        decoder.flags2 = av.codec.context.Flags2.FAST
        decoder.thread_type = "SLICE"  # Never FRAME threading for live FPV
        decoder.thread_count = 1       # 1 thread = 0 lookahead delay buffer
        return decoder

    def _reset_stream_state(self, reason: str = "") -> None:
        """Purges any partial chunks, resets sequence counters, and recreates the H.264 decoder."""
        self._frame_parts.clear()
        self._frame_totals.clear()
        self._frame_timestamps.clear()
        self._last_completed_seq = None
        self._frame_times.clear()
        self._bytes_received = 0
        self._current_bitrate_kbps = 0.0

        try:
            self._decoder = self._create_decoder()
        except Exception as e:
            self._log(f"[WARN] Error recreating decoder: {e}")

        if reason:
            self._log(f"[RECEIVER] Stream state reset ({reason})")

    @property
    def last_sender_str(self) -> str:
        if self._last_sender_addr:
            return f"{self._last_sender_addr[0]}:{self._last_sender_addr[1]}"
        return "Unknown"

    def _handle_connection(self, addr: tuple, reason: str = "") -> None:
        with self._lock:
            was_connected = self.is_connected
            self._last_sender_addr = addr
            self.last_packet_time = time.time()
            self.last_connected_time = self.last_packet_time

            if not was_connected:
                self.is_connected = True
                self.connection_state = ConnectionState.CONNECTED
                self.disconnect_reason = ""
                self._reset_stream_state("reconnect")
                sender_str = f"{addr[0]}:{addr[1]}"
                self._log(f"[LINK CONNECTED] Stream linked with {sender_str} ({reason})")
                if self.on_connection_change:
                    try:
                        self.on_connection_change(True, reason, sender_str)
                    except Exception as e:
                        self._log(f"[WARN] Connection callback error: {e}")

    def _handle_disconnection(self, reason: str = "") -> None:
        with self._lock:
            if self.is_connected:
                self.is_connected = False
                self.connection_state = ConnectionState.RECONNECTING
                self.disconnect_reason = reason
                self._reset_stream_state("disconnect")
                sender_str = self.last_sender_str
                self._log(f"[LINK DISCONNECTED] {reason} (Transmitter: {sender_str}). Waiting for transmitter...")
                if self.on_connection_change:
                    try:
                        self.on_connection_change(False, reason, sender_str)
                    except Exception as e:
                        self._log(f"[WARN] Disconnection callback error: {e}")

    def send_feedback(self, latency_ms: float) -> None:
        """Sends latency feedback back to transmitter for dynamic quality scaling."""
        if self._sock and self._last_sender_addr and self.is_connected:
            try:
                lat_int = int(max(0, min(65535, latency_ms)))
                self._sock.sendto(struct.pack(">H", lat_int), self._last_sender_addr)
            except Exception:
                pass

    def start(self) -> None:
        if self._running.is_set():
            return
        self._open_socket()
        self._running.set()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True, name="UDP-Receiver-Thread")
        self._thread.start()
        self._log(f"Receiver listening on {self.bind_ip}:{self.port}")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._close_socket()
        self._log("Receiver stopped")

    def _open_socket(self) -> None:
        try:
            # Dual-stack IPv6 socket (receives both IPv6 and IPv4 traffic)
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            sock.settimeout(0.1)
            bind_addr = self.bind_ip if self.bind_ip not in ("0.0.0.0", "") else "::"
            sock.bind((bind_addr, self.port))
            self._sock = sock
        except Exception:
            # Fallback to pure IPv4 socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            sock.settimeout(0.1)
            bind_addr = self.bind_ip if self.bind_ip != "::" else "0.0.0.0"
            sock.bind((bind_addr, self.port))
            self._sock = sock

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _recv_loop(self) -> None:
        sock = self._sock
        if not sock:
            return

        while self._running.is_set():
            now = time.time()

            # Connection Watchdog: If no packet received for > 1.8 seconds, signal is lost
            if self.is_connected and (now - self.last_packet_time > 1.8):
                self._handle_disconnection("Signal lost / Watchdog timeout (>1.8s)")

            try:
                data, addr = sock.recvfrom(2048)
                now = time.time()
                self._last_sender_addr = addr
            except socket.timeout:
                continue
            except OSError:
                break

            if not data:
                continue

            # Handle Control / Keepalive packets
            if data[0] == CTRL_MAGIC:
                # Goodbye or App Close packet
                if len(data) >= 4 and data[:3] == b"\xFF\xDE\xAD":
                    reason = "Transmitter stopped transmitting" if data[3] == 0x01 else "Transmitter app closed"
                    self._handle_disconnection(reason)
                    continue

                # Hello packet (Transmitter starting)
                if len(data) >= 4 and data[:4] == PACKET_HELLO:
                    self._handle_connection(addr, "Transmitter Hello received")
                    continue

                # Keep-alive packet (Modem ping or idle keepalive)
                if len(data) >= 4 and data[:4] == PACKET_KEEP_ALIVE:
                    self.last_packet_time = now
                    continue

                continue

            if len(data) < HEADER_SIZE:
                continue

            # Valid video packet! Ensure connection is active
            if not self.is_connected:
                self._handle_connection(addr, "Incoming video stream detected")

            self.last_packet_time = now
            self._bytes_received += len(data)

            # Parse 16-byte header:
            # 0..3: Frame Sequence (uint32)
            # 4..5: Chunk Index (uint16)
            # 6..7: Total Chunks (uint16)
            # 8..15: Timestamp in ms (uint64)
            frame_seq = struct.unpack_from(">I", data, 0)[0]
            chunk_idx = struct.unpack_from(">H", data, 4)[0]
            total_chunks = struct.unpack_from(">H", data, 6)[0]
            ts_ms = struct.unpack_from(">Q", data, 8)[0]
            payload = data[HEADER_SIZE:]

            with self._lock:
                # Discard stale packets from frames already completed
                if self._last_completed_seq is not None:
                    # Allow sequence rollover or reset if diff is large
                    diff = self._last_completed_seq - frame_seq
                    if 0 <= diff < 100:
                        continue

                # Store chunk
                if frame_seq not in self._frame_parts:
                    # Evict oldest incomplete frames if buffer has more than 5 frames
                    if len(self._frame_parts) > 5:
                        oldest = min(self._frame_parts.keys())
                        self._dropped_frames += 1
                        self._frame_parts.pop(oldest, None)
                        self._frame_totals.pop(oldest, None)
                        self._frame_timestamps.pop(oldest, None)

                    self._frame_parts[frame_seq] = {}
                    self._frame_totals[frame_seq] = total_chunks
                    self._frame_timestamps[frame_seq] = ts_ms

                self._frame_parts[frame_seq][chunk_idx] = payload

                # Check if all chunks for this frame have arrived
                if len(self._frame_parts[frame_seq]) == total_chunks:
                    # Reconstruct full frame in exact 0..total_chunks-1 sequence order
                    full_frame = b"".join(self._frame_parts[frame_seq][i] for i in range(total_chunks))
                    frame_ts = self._frame_timestamps.pop(frame_seq, ts_ms)
                    self._frame_parts.pop(frame_seq, None)
                    self._frame_totals.pop(frame_seq, None)

                    # Track dropped frame gaps
                    if self._last_completed_seq is not None:
                        gap = frame_seq - (self._last_completed_seq + 1)
                        if 0 < gap < 1000:
                            self._dropped_frames += gap

                    self._last_completed_seq = frame_seq
                    self._process_frame(frame_seq, full_frame, frame_ts)

    def _process_frame(self, seq: int, raw_bytes: bytes, sender_ts_ms: int) -> None:
        t_decode_start = time.perf_counter()

        annexb_bytes = self._to_annexb(raw_bytes)
        now_ms = int(time.time() * 1000)
        transit_latency = float(now_ms - sender_ts_ms)

        try:
            packet = av.packet.Packet(annexb_bytes)
            frames = self._decoder.decode(packet)

            for frame in frames:
                t_decode_end = time.perf_counter()
                decode_ms = (t_decode_end - t_decode_start) * 1000.0

                # Convert to BGR for OpenCV / rendering
                img = frame.to_ndarray(format="bgr24")
                h, w = img.shape[:2]

                self._total_frames += 1
                fps = self._update_fps()
                bitrate = self._update_bitrate()

                stats = FrameStats(
                    sequence=seq,
                    sender_timestamp_ms=sender_ts_ms,
                    transit_latency_ms=transit_latency,
                    decode_time_ms=decode_ms,
                    fps=fps,
                    bitrate_kbps=bitrate,
                    width=w,
                    height=h,
                    total_frames=self._total_frames,
                    dropped_frames=self._dropped_frames,
                )
                self._last_stats = stats

                if self.on_frame and self.is_connected:
                    self.on_frame(img, stats)

        except (av.AVError, ValueError):
            pass

    def _to_annexb(self, data: bytes) -> bytes:
        if data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01"):
            return data

        out = bytearray()
        i = 0
        n = len(data)
        while i + 4 <= n:
            nal_len = struct.unpack_from(">I", data, i)[0]
            i += 4
            if nal_len == 0 or i + nal_len > n:
                return data
            out += START_CODE
            out += data[i : i + nal_len]
            i += nal_len
        return bytes(out) if out else data

    def _update_fps(self) -> float:
        now = time.time()
        self._frame_times.append(now)
        cutoff = now - 1.5
        self._frame_times = [t for t in self._frame_times if t >= cutoff]
        if len(self._frame_times) < 2:
            return 0.0
        duration = self._frame_times[-1] - self._frame_times[0]
        return len(self._frame_times) / duration if duration > 0 else 0.0

    def _update_bitrate(self) -> float:
        now = time.time()
        elapsed = now - self._last_bitrate_time
        if elapsed >= 1.0:
            self._current_bitrate_kbps = (self._bytes_received * 8.0) / (elapsed * 1000.0)
            self._bytes_received = 0
            self._last_bitrate_time = now
        return self._current_bitrate_kbps

    def get_latest_stats(self) -> FrameStats:
        return self._last_stats

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)

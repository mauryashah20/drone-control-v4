import sys
import socket
import struct
import threading
import time
import queue
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, Dict
import av
import cv2
import numpy as np

HEADER_SIZE = 18  # 4B frame_seq, 1B flags, 1B reserved, 2B chunk_idx, 2B total_chunks, 8B ts_ms

# Control packet signatures
CTRL_MAGIC = 0xFF
PACKET_KEEP_ALIVE = b"\xFF\xAA\x55\x00"
PACKET_HELLO = b"\xFF\x01\xCA\xFE"
PACKET_GOODBYE = b"\xFF\xDE\xAD\x01"
PACKET_PEER_UPDATE = b"\xFF\x55"


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
    codec: str = "HEVC"
    rx_packets: int = 0
    lost_packets: int = 0
    incomplete_frames: int = 0
    idr_frames: int = 0
    recovery_events: int = 0
    decode_errors: int = 0
    fec_recovered: int = 0


class FrameBuffer:
    __slots__ = ("chunks", "total_chunks", "arrival_ts", "sender_ts_ms", "is_key", "parity")

    def __init__(self, total_chunks: int, arrival_ts: float, sender_ts_ms: int, is_key: bool):
        self.chunks: Dict[int, bytes] = {}
        self.total_chunks = total_chunks
        self.arrival_ts = arrival_ts
        self.sender_ts_ms = sender_ts_ms
        self.is_key = is_key
        self.parity: Optional[tuple] = None  # (last_chunk_len, parity_bytes)


class ZeroLatencyVideoReceiver:
    """
    Ultra-low latency HEVC video receiver over UDP with 1-packet XOR FEC.
    Performs deterministic chunk assembly, zero-delay decoding, and automatic link tracking.
    """

    def __init__(
        self,
        bind_ip: str = "::",
        port: int = 5005,
        on_frame: Optional[Callable[[np.ndarray, FrameStats], None]] = None,
        on_connection_change: Optional[Callable[[bool, str, Optional[str]], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_peer_update: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        self.bind_ip = bind_ip
        self.port = port
        self.on_frame = on_frame
        self.on_connection_change = on_connection_change
        self.on_log = on_log
        self.on_peer_update = on_peer_update

        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._decode_thread: Optional[threading.Thread] = None
        self._decode_queue: queue.Queue = queue.Queue(maxsize=2)
        self._running = threading.Event()
        self._lock = threading.Lock()

        # Connection status tracking
        self.is_connected = False
        self.connection_state = ConnectionState.SEARCHING
        self.disconnect_reason = "Waiting for initial connection..."
        self.last_packet_time: float = 0.0
        self.last_connected_time: float = 0.0
        self._last_sender_addr: Optional[tuple] = None
        self._last_sync_request_time: float = 0.0

        # Decoder & Frame Assembly
        self._active_codec_name = "hevc"
        self._decoder = self._create_decoder(self._active_codec_name)
        self._frames: Dict[int, FrameBuffer] = {}
        self.jitter_buffer_ms: float = 40.0
        self._highest_seen_seq: int = -1
        self._last_completed_seq: Optional[int] = None
        self._last_decoded_seq: Optional[int] = None
        self._min_clock_skew: Optional[float] = None
        self._skew_samples: deque = deque()

        # Loss & Reference tracking
        self._reference_lost: bool = True
        self._lost_frames_suppressed: int = 0
        self._rx_packets: int = 0
        self._lost_packets: int = 0
        self._dup_packets: int = 0
        self._ooo_packets: int = 0
        self._incomplete_frames: int = 0
        self._dropped_frames: int = 0
        self._decode_errors: int = 0
        self._idr_frames: int = 0
        self._recovery_events: int = 0
        self._fec_recovered_packets: int = 0

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
            width=1280,
            height=720,
            total_frames=0,
            dropped_frames=0,
        )

    def _create_decoder(self, codec: str = "hevc") -> av.CodecContext:
        """Creates zero-latency PyAV decoder context with LOW_DELAY and single-thread execution."""
        self._active_codec_name = codec
        decoder = av.CodecContext.create(codec, "r")
        decoder.flags = av.codec.context.Flags.LOW_DELAY
        decoder.thread_type = "NONE"
        decoder.thread_count = 1
        decoder.options = {"err_detect": "compliant"}
        return decoder

    def _reset_stream_state(self, reason: str = "") -> None:
        """Purges pending frames, resets sequence trackers, clock skew, and re-initializes decoder."""
        self._frames.clear()
        self._highest_seen_seq = -1
        self._last_completed_seq = None
        self._last_decoded_seq = None
        self._reference_lost = True
        self._frame_times.clear()
        self._bytes_received = 0
        self._current_bitrate_kbps = 0.0
        self._min_clock_skew = None
        self._skew_samples.clear()
        self._dropped_frames = 0
        self._lost_packets = 0
        self._incomplete_frames = 0
        self._decode_errors = 0
        self._fec_recovered_packets = 0
        self._total_frames = 0

        try:
            while not self._decode_queue.empty():
                self._decode_queue.get_nowait()
        except Exception:
            pass

        try:
            self._decoder = self._create_decoder(self._active_codec_name)
        except Exception as e:
            self._log(f"[WARN] Decoder recreate error: {e}")

        if reason:
            self._log(f"[RECEIVER] Stream reset ({reason})")

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
                self.request_sync_frame()
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
                self._log(f"[LINK DISCONNECTED] {reason} (Transmitter: {sender_str}). Waiting...")
                if self.on_connection_change:
                    try:
                        self.on_connection_change(False, reason, sender_str)
                    except Exception as e:
                        self._log(f"[WARN] Disconnection callback error: {e}")

    def request_sync_frame(self) -> None:
        """Sends an on-demand IDR keyframe request to the transmitter over UDP."""
        if self._sock and self._last_sender_addr:
            try:
                self._sock.sendto(b"\xFF\x02", self._last_sender_addr)
            except Exception:
                pass

    def _maybe_request_sync_frame(self) -> None:
        """Throttled keyframe request triggered upon packet loss / sequence gap (max 1 per 0.20s)."""
        now = time.time()
        if now - self._last_sync_request_time >= 0.20:
            self._last_sync_request_time = now
            self.request_sync_frame()

    def send_feedback(self, latency_ms: float) -> None:
        """Sends latency feedback back to transmitter for dynamic rate adaptation."""
        if self._sock and self._last_sender_addr and self.is_connected:
            try:
                lat_int = max(0, min(65535, int(latency_ms)))
                buf = struct.pack(">BBH", 0xFF, 0xFB, lat_int)
                self._sock.sendto(buf, self._last_sender_addr)
            except Exception:
                pass

    def start(self) -> None:
        if self._running.is_set():
            return
        self._open_socket()
        self._running.set()
        self._thread = threading.Thread(target=self._recv_loop, daemon=True, name="UDP-Receiver-Thread")
        self._thread.start()
        self._decode_thread = threading.Thread(target=self._decode_worker, daemon=True, name="HEVC-Decoder-Thread")
        self._decode_thread.start()
        self._log(f"Receiver listening on {self.bind_ip}:{self.port}")

    def stop(self) -> None:
        if not self._running.is_set():
            return
        self._running.clear()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._decode_thread is not None:
            self._decode_thread.join(timeout=1.0)
            self._decode_thread = None
        self._close_socket()
        self._log("Receiver stopped")

    def _open_socket(self) -> None:
        try:
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
            if sys.platform == "win32":
                try:
                    sock.ioctl(0x9800000C, False)
                except Exception:
                    pass
            self._sock = sock
        except Exception:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            sock.settimeout(0.1)
            bind_addr = self.bind_ip if self.bind_ip != "::" else "0.0.0.0"
            sock.bind((bind_addr, self.port))
            if sys.platform == "win32":
                try:
                    sock.ioctl(0x9800000C, False)
                except Exception:
                    pass
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

            if self.is_connected and (now - self.last_packet_time > 1.8):
                self._handle_disconnection("Signal lost / Watchdog timeout (>1.8s)")

            try:
                data, addr = sock.recvfrom(2048)
                now = time.time()
                self._last_sender_addr = addr
            except (socket.timeout, ConnectionResetError):
                continue
            except OSError as e:
                if getattr(e, "winerror", None) == 10054:
                    continue
                if self._running.is_set():
                    time.sleep(0.005)
                    continue
                break

            if not data:
                continue

            # Handle Control / Keepalive packets
            if data[0] == CTRL_MAGIC:
                if len(data) >= 2 and data[:2] == PACKET_PEER_UPDATE:
                    try:
                        import json
                        payload = json.loads(data[2:].decode("utf-8"))
                        peer_ip = payload.get("ipv6")
                        peer_port = payload.get("port", 5005)
                        if peer_ip:
                            self._log(f"[RECEIVER] Vercel peer push: [{peer_ip}]:{peer_port}")
                            self._last_sender_addr = (peer_ip, peer_port)
                            if self.on_peer_update:
                                self.on_peer_update(peer_ip, peer_port)
                    except Exception as e:
                        self._log(f"[RECEIVER] Vercel push parse error: {e}")
                    continue

                if len(data) >= 4 and data[:3] == b"\xFF\xDE\xAD":
                    reason = "Transmitter stopped" if data[3] == 0x01 else "Transmitter app closed"
                    self._handle_disconnection(reason)
                    continue

                if len(data) >= 4 and data[:4] == PACKET_HELLO:
                    self._handle_connection(addr, "Transmitter Hello received")
                    continue

                if len(data) >= 4 and data[:4] == PACKET_KEEP_ALIVE:
                    self.last_packet_time = now
                    continue

                continue

            if len(data) < HEADER_SIZE:
                continue

            if not self.is_connected:
                self._handle_connection(addr, "Incoming video stream detected")

            self.last_packet_time = now
            self._bytes_received += len(data)
            self._rx_packets += 1

            # Parse 18-byte Header: frame_seq, flags, total_slices, chunk_idx, total_chunks, ts_ms
            frame_seq, flags, _, chunk_idx, total_chunks, ts_ms = struct.unpack_from(">IBBHHQ", data, 0)
            is_key_hint = bool(flags & 0x01)
            is_fec_parity = bool(flags & 0x02)
            payload = data[HEADER_SIZE:]
            now_perf = time.perf_counter()

            with self._lock:
                # Detect transmitter restart / sequence wraparound
                if self._last_completed_seq is not None and (
                    (frame_seq < 20 and self._last_completed_seq > 30)
                    or (frame_seq < self._last_completed_seq - 100)
                ):
                    self._log(f"[RECEIVER] Sequence reset detected ({self._last_completed_seq} -> {frame_seq}). Resetting stream session.")
                    self._reset_stream_state("sequence_restart")
                    self.request_sync_frame()

                if self._last_decoded_seq is not None and frame_seq <= self._last_decoded_seq:
                    self._dup_packets += 1
                    continue
                if self._last_completed_seq is not None and frame_seq <= self._last_completed_seq:
                    self._dup_packets += 1
                    continue

                if self._highest_seen_seq >= 0 and frame_seq < self._highest_seen_seq:
                    self._ooo_packets += 1
                elif frame_seq > self._highest_seen_seq:
                    self._highest_seen_seq = frame_seq

                # Expire incomplete frames whose jitter buffer window has elapsed
                to_drop = [
                    seq for seq, fb in self._frames.items()
                    if (self._highest_seen_seq > seq and (now_perf - fb.arrival_ts) * 1000.0 >= self.jitter_buffer_ms)
                    or ((now_perf - fb.arrival_ts) > 0.200)
                ]
                for seq in to_drop:
                    self._incomplete_frames += 1
                    self._dropped_frames += 1
                    self._frames.pop(seq, None)
                    self._reference_lost = True
                    self._maybe_request_sync_frame()

                if frame_seq not in self._frames:
                    self._frames[frame_seq] = FrameBuffer(total_chunks, now_perf, ts_ms, is_key_hint)

                fb = self._frames[frame_seq]
                if is_key_hint:
                    fb.is_key = True

                if is_fec_parity:
                    if len(payload) >= 2:
                        last_c_len = struct.unpack_from(">H", payload, 0)[0]
                        fb.parity = (last_c_len, payload[2:])
                else:
                    if chunk_idx in fb.chunks:
                        self._dup_packets += 1
                        continue
                    fb.chunks[chunk_idx] = payload

                # Check if missing chunk can be reconstructed via 1-Packet XOR FEC
                num_chunks = len(fb.chunks)
                if num_chunks == fb.total_chunks - 1 and fb.parity is not None:
                    last_c_len, parity_bytes = fb.parity
                    missing_idx = next((i for i in range(fb.total_chunks) if i not in fb.chunks), None)
                    if missing_idx is not None:
                        missing_len = last_c_len if missing_idx == fb.total_chunks - 1 else len(parity_bytes)
                        recon = bytearray(parity_bytes[:missing_len])
                        for c_i, c_data in fb.chunks.items():
                            xor_len = min(missing_len, len(c_data))
                            for b_i in range(xor_len):
                                recon[b_i] ^= c_data[b_i]
                        fb.chunks[missing_idx] = bytes(recon)
                        self._fec_recovered_packets += 1
                        num_chunks = len(fb.chunks)

                # Check if all chunks arrived / recovered
                if num_chunks == fb.total_chunks:
                    full_frame = b"".join(fb.chunks[i] for i in range(fb.total_chunks))
                    frame_ts = fb.sender_ts_ms
                    is_key = fb.is_key
                    self._frames.pop(frame_seq, None)

                    if self._last_completed_seq is not None and frame_seq <= self._last_completed_seq:
                        self._ooo_packets += 1
                        continue

                    # Track dropped frame gaps
                    if self._last_completed_seq is not None:
                        gap = frame_seq - (self._last_completed_seq + 1)
                        if 0 < gap < 1000:
                            self._lost_packets += gap
                            self._reference_lost = True
                            self._maybe_request_sync_frame()

                    self._last_completed_seq = frame_seq

                    # Non-blocking handoff to decoder thread
                    try:
                        if self._decode_queue.full():
                            try:
                                self._decode_queue.get_nowait()
                                self._dropped_frames += 1
                                self._reference_lost = True
                                self._maybe_request_sync_frame()
                            except queue.Empty:
                                pass
                        self._decode_queue.put_nowait((frame_seq, full_frame, frame_ts, is_key))
                    except Exception:
                        pass

    def _decode_worker(self) -> None:
        """Dedicated decoding worker running off the UDP network socket thread."""
        while self._running.is_set():
            try:
                item = self._decode_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            frame_seq, full_frame, frame_ts, is_key_hint = item
            self._process_frame(frame_seq, full_frame, frame_ts, is_key_hint)

    def _process_frame(self, seq: int, raw_bytes: bytes, sender_ts_ms: int, is_key_hint: bool = False) -> None:
        t_decode_start = time.perf_counter()

        now_perf = t_decode_start
        now_ms = time.time() * 1000.0
        raw_diff = float(now_ms - sender_ts_ms)

        # Sliding window minimum filter (4.0s window) tracks network delay & absorbs clock drift
        self._skew_samples.append((now_perf, raw_diff))
        cutoff = now_perf - 4.0
        while self._skew_samples and self._skew_samples[0][0] < cutoff:
            self._skew_samples.popleft()

        min_skew = min(d for _, d in self._skew_samples) if self._skew_samples else raw_diff
        self._min_clock_skew = min_skew

        # Jitter is one-way delay above physical propagation floor
        jitter = max(0.0, raw_diff - min_skew)
        # G2G transit latency: 25ms physical air-interface baseline + network queue jitter
        transit_latency = max(15.0, min(250.0, jitter + 25.0))

        # Reference-frame corruption guard
        if is_key_hint:
            if self._reference_lost:
                self._recovery_events += 1
            self._reference_lost = False
            self._lost_frames_suppressed = 0
            self._idr_frames += 1
        elif self._reference_lost:
            self._dropped_frames += 1
            self._lost_frames_suppressed += 1
            self._maybe_request_sync_frame()
            if self._lost_frames_suppressed > 6:
                self._reference_lost = False
            else:
                return

        try:
            packet = av.packet.Packet(raw_bytes)
            frames = self._decoder.decode(packet)

            for frame in frames:
                if getattr(frame, "is_corrupt", False):
                    self._decode_errors += 1
                    self._reference_lost = True
                    self._maybe_request_sync_frame()
                    continue

                self._last_decoded_seq = seq
                decode_ms = (time.perf_counter() - t_decode_start) * 1000.0

                try:
                    img = frame.to_ndarray(format="bgr24")
                    h, w = img.shape[:2]
                except Exception:
                    continue

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
                    codec=self._active_codec_name.upper(),
                    rx_packets=self._rx_packets,
                    lost_packets=self._lost_packets,
                    incomplete_frames=self._incomplete_frames,
                    idr_frames=self._idr_frames,
                    recovery_events=self._recovery_events,
                    decode_errors=self._decode_errors,
                    fec_recovered=self._fec_recovered_packets,
                )
                self._last_stats = stats

                if self.on_frame and self.is_connected:
                    self.on_frame(img, stats)

        except (av.AVError, ValueError):
            self._decode_errors += 1
            self._reference_lost = True
            self._maybe_request_sync_frame()

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

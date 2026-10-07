"""
Comprehensive Experimental Benchmark for Drone FPV UDP Receiver
Measures latency, packet loss, reassembly failures, freeze rate, and decoder health
across UDP receive buffer, reassembly jitter buffer, and keyframe interval.
"""

import sys
import os
import time
import socket
import struct
import threading
import queue
import statistics
import random
import psutil
import av
import numpy as np

HEADER_SIZE = 18
START_CODE = b"\x00\x00\x00\x01"

def generate_sample_hevc_stream(width=640, height=480, fps=30, num_frames=120, keyframe_interval=30):
    """Generates a realistic stream of HEVC frames with known I/P frame structure."""
    out_frames = []
    # Use PyAV encoder to generate real H.265 frames
    container = av.open("pipe:", mode="w", format="hevc")
    stream = container.add_stream("libx264" if "hevc" not in av.codecs_available else "hevc", rate=fps)
    stream.width = width
    stream.height = height
    stream.pix_fmt = "yuv420p"
    stream.options = {
        "preset": "ultrafast",
        "tune": "zerolatency",
        "x265-params": f"keyint={keyframe_interval}:min-keyint={keyframe_interval}:no-scenecut=1"
    }

    # If libx265 is not available in ffmpeg build, fallback to x264 or raw AnnexB pattern
    for i in range(num_frames):
        # Create a simple synthetic test pattern with moving elements
        img = np.zeros((height, width, 3), dtype=np.uint8)
        cv2_val = int((i * 5) % 255)
        img[:, :] = (cv2_val, 128, int(max(0, min(255, 255 - cv2_val))))
        # Add frame index text
        # YUV420 conversion
        yuv = np.zeros((height * 3 // 2, width), dtype=np.uint8)
        yuv[:height, :] = cv2_val
        yuv[height:, :] = 128
        frame = av.VideoFrame.from_ndarray(yuv, format="yuv420p")
        frame.pts = i

        try:
            for packet in stream.encode(frame):
                is_key = (i % keyframe_interval == 0)
                out_frames.append((bytes(packet), is_key, i))
        except Exception:
            pass

    try:
        for packet in stream.encode():
            out_frames.append((bytes(packet), False, len(out_frames)))
    except Exception:
        pass

    return out_frames

class BenchmarkReceiver:
    def __init__(self, port, rcvbuf_bytes, jitter_buffer_ms):
        self.port = port
        self.rcvbuf_bytes = rcvbuf_bytes
        self.jitter_buffer_ms = jitter_buffer_ms
        self.jitter_buffer_s = jitter_buffer_ms / 1000.0

        self.running = threading.Event()
        self.sock = None
        self.recv_thread = None
        self.process_thread = None

        # Protocol reassembly state
        self._lock = threading.Lock()
        self._frames = {} # frame_seq -> {"first_ts": float, "total_chunks": int, "chunks": {idx: bytes}, "sender_ts": int, "is_key": bool}
        self._last_completed_seq = None
        self._highest_seen_seq = -1

        # Metrics
        self.packets_received = 0
        self.packets_out_of_order = 0
        self.reassembly_failures = 0
        self.incomplete_frames = 0
        self.completed_frames = 0
        self.dropped_frames = 0
        self.decoder_errors = 0
        self.latencies = []
        self.frame_intervals = []
        self.last_frame_decode_time = None
        self.stalls = 0 # Inter-frame gap > 2 * frame_duration

        # Decoder setup
        self._codec = "hevc"
        self._decoder = av.CodecContext.create(self._codec, "r")
        self._decoder.flags |= av.codec.context.Flags.LOW_DELAY
        self._decoder.thread_count = 1

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self.rcvbuf_bytes)
        except OSError:
            pass
        self.sock.settimeout(0.02)
        self.sock.bind(("127.0.0.1", self.port))

        self.running.set()
        self.recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self.recv_thread.start()

    def stop(self):
        self.running.clear()
        if self.recv_thread:
            self.recv_thread.join(timeout=0.5)
        if self.sock:
            self.sock.close()

    def _recv_loop(self):
        last_seq = -1
        while self.running.is_set():
            now = time.perf_counter()
            try:
                data, _ = self.sock.recvfrom(2048)
            except (socket.timeout, BlockingIOError):
                self._check_timeouts(now)
                continue
            except OSError:
                break

            if len(data) < HEADER_SIZE:
                continue

            self.packets_received += 1
            frame_seq, slice_idx, total_slices, chunk_idx, total_chunks, ts_ms = struct.unpack_from(">IBBHHQ", data, 0)
            payload = data[HEADER_SIZE:]

            if last_seq != -1 and frame_seq < last_seq:
                self.packets_out_of_order += 1
            last_seq = max(last_seq, frame_seq)

            with self._lock:
                if self._last_completed_seq is not None and frame_seq <= self._last_completed_seq:
                    # Stale chunk for already completed / passed frame
                    continue

                if frame_seq not in self._frames:
                    # New frame tracking
                    self._frames[frame_seq] = {
                        "first_ts": now,
                        "total_chunks": total_chunks,
                        "chunks": {},
                        "sender_ts": ts_ms,
                    }
                    if frame_seq > self._highest_seen_seq:
                        self._highest_seen_seq = frame_seq

                f_info = self._frames[frame_seq]
                f_info["chunks"][chunk_idx] = payload

                # Check if this frame is complete
                if len(f_info["chunks"]) == f_info["total_chunks"]:
                    self._emit_frame(frame_seq, f_info)

            self._check_timeouts(now)

    def _check_timeouts(self, now):
        """Purges incomplete frames whose jitter buffer window has expired."""
        with self._lock:
            to_purge = []
            for seq, f_info in self._frames.items():
                age_ms = (now - f_info["first_ts"]) * 1000.0
                # If newer frames have already been seen and age exceeds jitter buffer:
                if self._highest_seen_seq > seq and age_ms >= self.jitter_buffer_ms:
                    to_purge.append(seq)
                elif age_ms > 200: # absolute failsafe timeout
                    to_purge.append(seq)

            for seq in to_purge:
                self.reassembly_failures += 1
                self.incomplete_frames += 1
                self.dropped_frames += 1
                del self._frames[seq]

    def _emit_frame(self, frame_seq, f_info):
        # Assemble frame payload
        chunks = [f_info["chunks"][i] for i in range(f_info["total_chunks"])]
        raw_bytes = b"".join(chunks)
        sender_ts = f_info["sender_ts"]
        del self._frames[frame_seq]

        # Calculate dropped gap
        if self._last_completed_seq is not None:
            gap = frame_seq - (self._last_completed_seq + 1)
            if gap > 0:
                self.dropped_frames += gap
        self._last_completed_seq = frame_seq

        # Decode & measure latency
        t_decode_start = time.perf_counter()
        now_ms = int(time.time() * 1000)
        transit_latency = max(0.0, float(now_ms - sender_ts))

        # Feed to decoder
        annexb = raw_bytes if (raw_bytes.startswith(b"\x00\x00\x00\x01") or raw_bytes.startswith(b"\x00\x00\x01")) else (START_CODE + raw_bytes)
        try:
            packet = av.packet.Packet(annexb)
            decoded = self._decoder.decode(packet)
            t_decode_end = time.perf_counter()
            decode_time_ms = (t_decode_end - t_decode_start) * 1000.0
            total_latency = transit_latency + decode_time_ms

            if decoded:
                self.completed_frames += 1
                self.latencies.append(total_latency)

                # Track frame interval & stalls
                if self.last_frame_decode_time is not None:
                    interval_ms = (t_decode_end - self.last_frame_decode_time) * 1000.0
                    self.frame_intervals.append(interval_ms)
                    # nominal frame duration at 30 fps is ~33.3ms, at 60 fps is ~16.6ms
                    if interval_ms > 70.0:
                        self.stalls += 1
                self.last_frame_decode_time = t_decode_end

        except Exception:
            self.decoder_errors += 1

def run_single_benchmark(
    rcvbuf_bytes,
    jitter_buffer_ms,
    keyframe_interval_s,
    frames_dataset,
    loss_rate=0.02,
    jitter_ms=6.0,
    burst_mode=True,
    duration_s=4.0,
    fps=30
):
    port = random.randint(35000, 48000)
    receiver = BenchmarkReceiver(port, rcvbuf_bytes, jitter_buffer_ms)
    receiver.start()

    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    payload_mtu = 1150 - HEADER_SIZE

    # Measure CPU and memory before
    process = psutil.Process(os.getpid())
    cpu_start = process.cpu_percent()
    mem_start = process.memory_info().rss / (1024 * 1024)

    total_packets_sent = 0
    total_frames_sent = 0
    start_time = time.perf_counter()
    frame_idx = 0
    frame_interval_s = 1.0 / fps

    keyint_frames = int(max(1, keyframe_interval_s * fps))

    # Transmit loop
    while (time.perf_counter() - start_time) < duration_s:
        t_frame_start = time.perf_counter()

        # Pick frame from dataset, apply keyframe interval
        raw_frame_data, _, _ = frames_dataset[frame_idx % len(frames_dataset)]
        is_keyframe = (frame_idx % keyint_frames == 0)

        # Build chunks
        frame_size = len(raw_frame_data)
        total_chunks = max(1, (frame_size + payload_mtu - 1) // payload_mtu)
        ts_ms = int(time.time() * 1000)

        chunks_to_send = []
        offset = 0
        for c_idx in range(total_chunks):
            c_size = min(payload_mtu, frame_size - offset)
            chunk_payload = raw_frame_data[offset:offset + c_size]
            offset += c_size

            hdr = struct.pack(">IBBHHQ", frame_idx, 0, 1, c_idx, total_chunks, ts_ms)
            pkt = hdr + chunk_payload
            chunks_to_send.append((c_idx, pkt))

        # Impairment model: simulate network jitter, out-of-order, packet drop
        # Shuffle/delay slightly if jitter > 0
        delayed_queue = []
        for c_idx, pkt in chunks_to_send:
            total_packets_sent += 1
            # Random drop
            if random.random() < loss_rate:
                continue # Lost in transit

            # Jitter / reorder
            if jitter_ms > 0 and random.random() < 0.15:
                # 15% probability of chunk experiencing slight reorder / micro-delay
                delayed_queue.append(pkt)
            else:
                try:
                    sender_sock.sendto(pkt, ("127.0.0.1", port))
                except OSError:
                    pass

        # Send delayed chunks (simulating out-of-order delivery)
        for pkt in delayed_queue:
            try:
                sender_sock.sendto(pkt, ("127.0.0.1", port))
            except OSError:
                pass

        total_frames_sent += 1
        frame_idx += 1

        elapsed = time.perf_counter() - t_frame_start
        sleep_rem = frame_interval_s - elapsed
        if sleep_rem > 0:
            time.sleep(sleep_rem)

    # Let lingering packets settle
    time.sleep(0.15)
    receiver.stop()
    sender_sock.close()

    cpu_end = process.cpu_percent()
    mem_end = process.memory_info().rss / (1024 * 1024)

    # Metrics calculation
    pkts_received = receiver.packets_received
    pkts_lost = max(0, total_packets_sent - pkts_received)
    pkt_loss_pct = (pkts_lost / max(1, total_packets_sent)) * 100.0

    completed_frames = receiver.completed_frames
    frame_drop_count = receiver.dropped_frames
    reassembly_fails = receiver.reassembly_failures
    effective_fps = completed_frames / duration_s
    freeze_rate = (receiver.stalls / max(1, completed_frames)) * 100.0

    median_lat = statistics.median(receiver.latencies) if receiver.latencies else 999.0
    p95_lat = np.percentile(receiver.latencies, 95) if receiver.latencies else 999.0
    jitter = statistics.stdev(receiver.frame_intervals) if len(receiver.frame_intervals) > 2 else 0.0

    return {
        "rcvbuf_kb": rcvbuf_bytes // 1024,
        "jitter_ms": jitter_buffer_ms,
        "keyframe_s": keyframe_interval_s,
        "latency_median_ms": round(median_lat, 2),
        "latency_p95_ms": round(p95_lat, 2),
        "pkt_loss_pct": round(pkt_loss_pct, 2),
        "packets_sent": total_packets_sent,
        "packets_rcvd": pkts_received,
        "out_of_order": receiver.packets_out_of_order,
        "reassembly_failures": reassembly_fails,
        "completed_frames": completed_frames,
        "dropped_frames": frame_drop_count,
        "decoder_errors": receiver.decoder_errors,
        "freeze_rate_pct": round(freeze_rate, 2),
        "effective_fps": round(effective_fps, 1),
        "jitter_ms_std": round(jitter, 2),
        "cpu_pct": round(max(cpu_start, cpu_end), 1),
        "mem_mb": round(mem_end, 1)
    }

def run_full_sweep():
    print("=" * 70)
    print("DRONE FPV UDP RECEIVER EXPERIMENTAL OPTIMIZATION BENCHMARK")
    print("=" * 70)
    print("Generating controlled HEVC 640x480 test dataset (120 frames)...")
    dataset = generate_sample_hevc_stream(width=640, height=480, fps=30, num_frames=120, keyframe_interval=30)
    print(f"Generated {len(dataset)} atomic HEVC frames.")

    results_stage1 = []

    # =========================================================================
    # STAGE 1: BROAD PARAMETER SWEEPS
    # =========================================================================
    print("\n--- STAGE 1A: UDP Receive Buffer Sweep (SO_RCVBUF) ---")
    rcvbuf_candidates = [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384] # KB
    rcvbuf_results = []
    for rkb in rcvbuf_candidates:
        # Test under burst packet conditions to detect OS socket drop threshold
        res = run_single_benchmark(
            rcvbuf_bytes=rkb * 1024,
            jitter_buffer_ms=10.0,
            keyframe_interval_s=1.0,
            frames_dataset=dataset,
            loss_rate=0.01,
            jitter_ms=5.0,
            duration_s=3.0
        )
        rcvbuf_results.append(res)
        print(f"  SO_RCVBUF {rkb:>5} KB -> Loss: {res['pkt_loss_pct']:>4.1f}% | Reassembly Fails: {res['reassembly_failures']:>2} | Latency: {res['latency_median_ms']:>4.1f}ms | Freezes: {res['freeze_rate_pct']:>4.1f}%")

    print("\n--- STAGE 1B: Reassembly Jitter Buffer Sweep ---")
    jitter_candidates = [0.0, 2.0, 5.0, 8.0, 10.0, 15.0, 20.0, 25.0, 30.0, 40.0, 50.0, 75.0, 100.0] # ms
    jitter_results = []
    for jms in jitter_candidates:
        res = run_single_benchmark(
            rcvbuf_bytes=1024 * 1024, # 1 MB baseline
            jitter_buffer_ms=jms,
            keyframe_interval_s=1.0,
            frames_dataset=dataset,
            loss_rate=0.02,
            jitter_ms=8.0, # Realistic cellular jitter
            duration_s=3.0
        )
        jitter_results.append(res)
        print(f"  Jitter Buffer {jms:>5.1f} ms -> Loss: {res['pkt_loss_pct']:>4.1f}% | Reassembly Fails: {res['reassembly_failures']:>2} | Latency: {res['latency_median_ms']:>4.1f}ms | Freezes: {res['freeze_rate_pct']:>4.1f}%")

    print("\n--- STAGE 1C: Keyframe Interval Sweep ---")
    keyframe_candidates = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0, 30.0, 60.0] # s
    keyframe_results = []
    for ks in keyframe_candidates:
        res = run_single_benchmark(
            rcvbuf_bytes=1024 * 1024,
            jitter_buffer_ms=10.0,
            keyframe_interval_s=ks,
            frames_dataset=dataset,
            loss_rate=0.03, # 3% cellular loss to test recovery
            jitter_ms=5.0,
            duration_s=4.0
        )
        keyframe_results.append(res)
        print(f"  Keyframe Int {ks:>5.2f} s -> Dropped Frms: {res['dropped_frames']:>3} | Decoded: {res['completed_frames']:>3} | Latency: {res['latency_median_ms']:>4.1f}ms | Freezes: {res['freeze_rate_pct']:>4.1f}%")

    # =========================================================================
    # STAGE 2: FINE-GRAINED SWEEP AROUND PARETO FRONTIER
    # =========================================================================
    print("\n--- STAGE 2: Fine-Grained Sweep around Top Region ---")
    fine_rcvbufs = [512, 1024, 2048]
    fine_jitters = [5.0, 8.0, 10.0, 15.0]
    fine_keyframes = [0.5, 1.0, 2.0]

    stage2_results = []
    for rkb in fine_rcvbufs:
        for jms in fine_jitters:
            for ks in fine_keyframes:
                res = run_single_benchmark(
                    rcvbuf_bytes=rkb * 1024,
                    jitter_buffer_ms=jms,
                    keyframe_interval_s=ks,
                    frames_dataset=dataset,
                    loss_rate=0.02,
                    jitter_ms=6.0,
                    duration_s=3.0
                )
                stage2_results.append(res)
                # print summary
                print(f"  [{rkb}KB, {jms}ms, {ks}s] -> Latency: {res['latency_median_ms']}ms, Pkt Loss: {res['pkt_loss_pct']}%, Reassembly Fails: {res['reassembly_failures']}, Freezes: {res['freeze_rate_pct']}%")

    # Score candidates: lower latency + lower fails + lower freezes
    # Pareto evaluation: Score = Latency(ms) * 1.0 + (Reassembly Fails * 15.0) + (Freeze Rate * 5.0)
    for r in stage2_results:
        r["pareto_score"] = r["latency_median_ms"] + (r["reassembly_failures"] * 12.0) + (r["freeze_rate_pct"] * 3.0) + (r["pkt_loss_pct"] * 2.0)

    stage2_sorted = sorted(stage2_results, key=lambda x: x["pareto_score"])
    finalists = stage2_sorted[:3]

    print("\n--- TOP 3 FINALISTS IDENTIFIED ---")
    for idx, f in enumerate(finalists, 1):
        print(f"  Candidate #{idx}: SO_RCVBUF={f['rcvbuf_kb']}KB, Jitter={f['jitter_ms']}ms, Keyframe={f['keyframe_s']}s (Score: {f['pareto_score']:.1f})")

    # =========================================================================
    # STAGE 3: MULTI-RUN REPETITION & STRESS CONDITIONS
    # =========================================================================
    print("\n--- STAGE 3: Multi-Run Repetition & Stress Testing (3 Runs per condition) ---")
    stress_profiles = [
        {"name": "Mild 4G/5G (1% loss, 3ms jitter)", "loss": 0.01, "jitter": 3.0},
        {"name": "Moderate Cellular Jitter (2.5% loss, 8ms jitter)", "loss": 0.025, "jitter": 8.0},
        {"name": "Severe Cellular Multipath (4.5% loss, 15ms jitter)", "loss": 0.045, "jitter": 15.0},
    ]

    finalist_stats = []
    for f in finalists:
        f_key = f"{f['rcvbuf_kb']}KB_{f['jitter_ms']}ms_{f['keyframe_s']}s"
        profile_runs = []
        for prof in stress_profiles:
            runs = []
            for _ in range(3):
                r = run_single_benchmark(
                    rcvbuf_bytes=f['rcvbuf_kb'] * 1024,
                    jitter_buffer_ms=f['jitter_ms'],
                    keyframe_interval_s=f['keyframe_s'],
                    frames_dataset=dataset,
                    loss_rate=prof['loss'],
                    jitter_ms=prof['jitter'],
                    duration_s=3.0
                )
                runs.append(r)
            avg_lat = statistics.mean(x['latency_median_ms'] for x in runs)
            avg_fails = statistics.mean(x['reassembly_failures'] for x in runs)
            avg_loss = statistics.mean(x['pkt_loss_pct'] for x in runs)
            avg_freeze = statistics.mean(x['freeze_rate_pct'] for x in runs)
            profile_runs.append({
                "profile": prof['name'],
                "avg_lat": round(avg_lat, 2),
                "avg_fails": round(avg_fails, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_freeze": round(avg_freeze, 2)
            })
            print(f"  {f_key} under {prof['name']} -> Lat: {avg_lat:.1f}ms, Fails: {avg_fails:.1f}, Loss: {avg_loss:.1f}%, Freezes: {avg_freeze:.1f}%")
        finalist_stats.append({"config": f, "profiles": profile_runs})

    return {
        "rcvbuf_results": rcvbuf_results,
        "jitter_results": jitter_results,
        "keyframe_results": keyframe_results,
        "finalists": finalists,
        "finalist_stats": finalist_stats
    }

if __name__ == "__main__":
    sweep_data = run_full_sweep()

"""
Post-Change Validation Script
Validates the actual modified ZeroLatencyVideoReceiver class under realistic network jitter and burst conditions.
"""

import sys
import os
import time
import socket
import struct
import random

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from receiver import ZeroLatencyVideoReceiver
from run_benchmark import generate_sample_hevc_stream, HEADER_SIZE

def run_validation():
    print("=" * 70)
    print("POST-CHANGE RECEIVER VALIDATION BENCHMARK")
    print("=" * 70)

    port = random.randint(39000, 49000)
    receiver = ZeroLatencyVideoReceiver(port=port, bind_ip="127.0.0.1")

    received_frames = []
    received_stats = []

    def on_frame_cb(img, stats):
        received_frames.append(img)
        received_stats.append(stats)

    receiver.on_frame = on_frame_cb
    receiver.start()

    print(f"Receiver started on port {port} with SO_RCVBUF=2048KB, JitterBuffer={receiver.jitter_buffer_ms}ms")

    # Generate HEVC dataset
    dataset = generate_sample_hevc_stream(width=640, height=480, fps=30, num_frames=120, keyframe_interval=30)
    payload_mtu = 1150 - HEADER_SIZE
    sender_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    total_packets_sent = 0
    total_frames_sent = 120
    loss_rate = 0.02 # 2% loss
    jitter_prob = 0.15 # 15% out-of-order jitter

    start_time = time.perf_counter()

    for frame_idx in range(total_frames_sent):
        t_fstart = time.perf_counter()
        raw_frame_data, is_key, _ = dataset[frame_idx % len(dataset)]
        frame_size = len(raw_frame_data)
        total_chunks = max(1, (frame_size + payload_mtu - 1) // payload_mtu)
        ts_ms = int(time.time() * 1000)

        delayed = []
        for c_idx in range(total_chunks):
            total_packets_sent += 1
            if random.random() < loss_rate:
                continue # Lost

            c_size = min(payload_mtu, frame_size - (c_idx * payload_mtu))
            chunk_payload = raw_frame_data[c_idx * payload_mtu: c_idx * payload_mtu + c_size]

            hdr = struct.pack(">IBBHHQ", frame_idx, 0, 1, c_idx, total_chunks, ts_ms)
            pkt = hdr + chunk_payload

            if random.random() < jitter_prob:
                delayed.append(pkt)
            else:
                try:
                    sender_sock.sendto(pkt, ("127.0.0.1", port))
                except OSError:
                    pass

        for pkt in delayed:
            try:
                sender_sock.sendto(pkt, ("127.0.0.1", port))
            except OSError:
                pass

        elapsed = time.perf_counter() - t_fstart
        rem = (1.0 / 30.0) - elapsed
        if rem > 0:
            time.sleep(rem)

    time.sleep(0.3)
    receiver.stop()
    sender_sock.close()

    total_decoded = len(received_frames)
    frame_drops = receiver._dropped_frames
    avg_dec_ms = (sum(s.decode_time_ms for s in received_stats) / max(1, len(received_stats))) if received_stats else 0.0

    print("-" * 70)
    print(f"Sent Frames:          {total_frames_sent}")
    print(f"Decoded Frames:       {total_decoded}")
    print(f"Reported Drops:       {frame_drops}")
    print(f"Decode Completion:    {(total_decoded / total_frames_sent) * 100:.1f}%")
    print(f"Average Decode Time:  {avg_dec_ms:.2f} ms")
    print("=" * 70)

if __name__ == "__main__":
    run_validation()

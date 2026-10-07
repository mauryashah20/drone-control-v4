import os
import sys
import time
import threading
import collections
from typing import Optional
import cv2
import numpy as np

# Ensure src is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from receiver import ZeroLatencyVideoReceiver, FrameStats, ConnectionState
from telemetry_router import TelemetryRouter, DroneTelemetryState
from ground_registry import GroundStationRegistryClient, detect_laptop_ipv6

WINDOW_NAME = "Drone FPV - Ultra-Low Latency Video Feed"


# ==============================================================================
# HACKER NEON + PITCH BLACK + BRIGHT GOLD SUPERCAR PALETTE (OpenCV BGR Format)
# ==============================================================================
COLOR_BLACK_PITCH = (8, 8, 12)          # Pitch black carbon background (#08080C)
COLOR_BLACK_PANEL = (14, 15, 20)        # HUD cockpit panel fill
COLOR_GOLD_BRIGHT = (0, 215, 255)       # Bright electric supercar gold (#FFD700 in BGR)
COLOR_GOLD_METALLIC = (45, 175, 215)    # Rich metallic gold (#D4AF37)
COLOR_GOLD_DARK = (20, 85, 110)         # Deep subtle gold divider
COLOR_NEON_GREEN = (57, 255, 20)        # Radiant hacker neon lime/green (#14FF39)
COLOR_NEON_CYAN = (255, 240, 0)         # Electric cyber neon cyan (#00F0FF)
COLOR_NEON_AMBER = (3, 183, 255)        # Warning gold/amber (#FFB703)
COLOR_NEON_RED = (77, 42, 255)          # Emergency alert red (#FF2A4D)
COLOR_WHITE_PURE = (255, 255, 255)      # Stark pure cockpit white
COLOR_WHITE_DIM = (220, 226, 235)       # Secondary white
COLOR_TEXT_MUTED = (148, 163, 184)      # Dim cockpit label


def draw_corner_brackets(img: np.ndarray, pt1: tuple, pt2: tuple, color: tuple = COLOR_GOLD_BRIGHT, length: int = 8, thickness: int = 1):
    """Draws supercar cockpit / precision targeting corner brackets."""
    x1, y1 = pt1
    x2, y2 = pt2
    # Top-Left
    cv2.line(img, (x1, y1), (x1 + length, y1), color, thickness, cv2.LINE_AA)
    cv2.line(img, (x1, y1), (x1, y1 + length), color, thickness, cv2.LINE_AA)
    # Top-Right
    cv2.line(img, (x2, y1), (x2 - length, y1), color, thickness, cv2.LINE_AA)
    cv2.line(img, (x2, y1), (x2, y1 + length), color, thickness, cv2.LINE_AA)
    # Bottom-Left
    cv2.line(img, (x1, y2), (x1 + length, y2), color, thickness, cv2.LINE_AA)
    cv2.line(img, (x1, y2), (x1, y2 - length), color, thickness, cv2.LINE_AA)
    # Bottom-Right
    cv2.line(img, (x2, y2), (x2 - length, y2), color, thickness, cv2.LINE_AA)
    cv2.line(img, (x2, y2), (x2, y2 - length), color, thickness, cv2.LINE_AA)


def draw_hud_box(img: np.ndarray, pt1: tuple, pt2: tuple, border_color: tuple = COLOR_GOLD_BRIGHT, fill_alpha: float = 0.5):
    """Draws a semi-transparent cockpit HUD telemetry box with bright gold borders."""
    x1, y1 = pt1
    x2, y2 = pt2
    h, w = img.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return

    overlay = img[y1:y2, x1:x2].copy()
    cv2.rectangle(img, (x1, y1), (x2, y2), COLOR_BLACK_PANEL, -1)
    cv2.addWeighted(img[y1:y2, x1:x2], fill_alpha, overlay, 1.0 - fill_alpha, 0, img[y1:y2, x1:x2])
    cv2.rectangle(img, (x1, y1), (x2, y2), COLOR_GOLD_DARK, 1, cv2.LINE_AA)
    draw_corner_brackets(img, (x1, y1), (x2, y2), color=border_color, length=7, thickness=1)


def draw_osd_text(img: np.ndarray, text: str, pos: tuple, color: tuple, scale: float = 0.45, thickness: int = 1):
    """Draws high-visibility FPV OSD text with a black outline shadow (no background box)."""
    x, y = pos
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, thickness, cv2.LINE_AA)


def draw_hud(
    img: np.ndarray,
    stats: FrameStats,
    g2g_latency_ms: float,
    sender_str: str,
    telem: Optional[DroneTelemetryState] = None,
    show_crosshair: bool = True,
) -> np.ndarray:
    """
    Renders an authentic, sleek FPV Goggles OSD (Betaflight / Supercar Telemetry style).
    Hacker Neon Green + Pitch Black + Stark White + Bright Gold Bordering.
    """
    h, w = img.shape[:2]

    # Latency color gradient
    if g2g_latency_ms < 100.0:
        lat_color = COLOR_NEON_GREEN     # Hacker Neon Green (< 100ms)
    elif g2g_latency_ms <= 150.0:
        lat_color = COLOR_GOLD_BRIGHT    # Bright Gold (100 - 150ms)
    else:
        lat_color = COLOR_NEON_RED       # Cyber Red (> 150ms)

    # 1. Top-Left: Cockpit Telemetry Box (Latency, Bitrate, Battery)
    box_w = 205
    box_h = 74 if (telem and telem.battery_voltage > 0) else 54
    draw_hud_box(img, (12, 10), (12 + box_w, 10 + box_h), border_color=COLOR_GOLD_BRIGHT, fill_alpha=0.6)

    # Telemetry chip tag
    draw_osd_text(img, "G2G:", (20, 30), COLOR_GOLD_BRIGHT, scale=0.45, thickness=1)
    draw_osd_text(img, f"{g2g_latency_ms:.0f}ms", (62, 30), lat_color, scale=0.55, thickness=2)

    draw_osd_text(img, "RATE:", (20, 50), COLOR_GOLD_BRIGHT, scale=0.40, thickness=1)
    draw_osd_text(img, f"{stats.bitrate_kbps:.0f} kbps", (66, 50), COLOR_WHITE_PURE, scale=0.42, thickness=1)

    if telem and telem.battery_voltage > 0:
        pct_str = f" ({telem.battery_remaining_pct}%)" if telem.battery_remaining_pct >= 0 else ""
        bat_color = COLOR_NEON_GREEN if (telem.battery_remaining_pct > 30 or telem.battery_voltage > 14.8) else (
            COLOR_GOLD_BRIGHT if (telem.battery_remaining_pct > 20 or telem.battery_voltage > 14.0) else COLOR_NEON_RED
        )
        draw_osd_text(img, "BAT:", (20, 70), COLOR_GOLD_BRIGHT, scale=0.40, thickness=1)
        draw_osd_text(img, f"{telem.battery_voltage:.1f}V{pct_str}", (58, 70), bat_color, scale=0.42, thickness=1)

    # 2. Top-Center: Live Status Badge Pill & Flight Mode
    status_text = f"LIVE  [{sender_str}]"
    (tw, _), _ = cv2.getTextSize(status_text, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)
    status_x = (w - tw) // 2
    pill_w = tw + 46
    pill_h = 44 if (telem and telem.is_heartbeat_fresh) else 28
    draw_hud_box(img, ((w - pill_w) // 2, 10), ((w + pill_w) // 2, 10 + pill_h), border_color=COLOR_GOLD_BRIGHT, fill_alpha=0.65)

    # Glowing Hacker Neon Dot
    cv2.circle(img, (status_x - 10, 24), 5, COLOR_NEON_GREEN, -1, cv2.LINE_AA)
    draw_osd_text(img, status_text, (status_x + 2, 28), COLOR_WHITE_PURE, scale=0.44, thickness=1)

    if telem and telem.is_heartbeat_fresh:
        armed_str = "ARMED" if telem.armed else "DISARMED"
        armed_color = COLOR_NEON_RED if telem.armed else COLOR_TEXT_MUTED
        mode_full_text = f"[{armed_str}]  {telem.flight_mode}"
        (mw, _), _ = cv2.getTextSize(mode_full_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
        draw_osd_text(img, mode_full_text, ((w - mw) // 2, 46), COLOR_GOLD_BRIGHT, scale=0.40, thickness=1)
    elif telem and telem.connected:
        mode_wait = "MAVLINK LOST"
        (mw, _), _ = cv2.getTextSize(mode_wait, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
        draw_osd_text(img, mode_wait, ((w - mw) // 2, 46), COLOR_NEON_AMBER, scale=0.40, thickness=1)

    # 3. Top-Right: Framerate & Decode Engine Cockpit Box
    dec_box_w = 170
    draw_hud_box(img, (w - dec_box_w - 12, 10), (w - 12, 64), border_color=COLOR_GOLD_BRIGHT, fill_alpha=0.6)

    fps_text = f"{stats.fps:.1f} FPS"
    fps_w = cv2.getTextSize(fps_text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 2)[0][0]
    draw_osd_text(img, fps_text, (w - fps_w - 22, 30), COLOR_NEON_GREEN, scale=0.55, thickness=2)

    dec_text = f"DEC: {stats.decode_time_ms:.1f}ms"
    dec_w = cv2.getTextSize(dec_text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)[0][0]
    draw_osd_text(img, dec_text, (w - dec_w - 22, 50), COLOR_WHITE_PURE, scale=0.42, thickness=1)

    # 4. Bottom Horizontal Telemetry Bar
    bar_y = h - 34
    draw_hud_box(img, (12, bar_y - 8), (w - 12, h - 8), border_color=COLOR_GOLD_BRIGHT, fill_alpha=0.7)

    # Bottom-Left: Frame Drops & Count
    draw_osd_text(img, f"DROPS: {stats.dropped_frames}  |  FRM: {stats.total_frames}", (22, h - 18), COLOR_TEXT_MUTED, scale=0.40, thickness=1)

    # Bottom-Center: Flight Instruments
    if telem and telem.is_heartbeat_fresh:
        inst_text = f"ALT: {telem.altitude_relative_m:.1f}m   SPD: {telem.groundspeed_mps:.1f}m/s   HDG: {telem.heading_deg:.0f}\u00b0"
        (iw, _), _ = cv2.getTextSize(inst_text, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)
        draw_osd_text(img, inst_text, ((w - iw) // 2, h - 18), COLOR_WHITE_PURE, scale=0.44, thickness=1)

    # Bottom-Right: Resolution, Codec & GPS Status
    codec_name = getattr(stats, "codec", "H.264")
    info_text = f"{stats.width}x{stats.height}  |  {codec_name}  |  BT-SERIAL"
    info_w = cv2.getTextSize(info_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)[0][0]
    draw_osd_text(img, info_text, (w - info_w - 22, h - 18), COLOR_GOLD_BRIGHT, scale=0.40, thickness=1)

    if telem and telem.is_heartbeat_fresh:
        gps_color = COLOR_NEON_GREEN if telem.gps_fix_type >= 3 else COLOR_GOLD_BRIGHT
        gps_text = f"{telem.gps_fix_str} ({telem.satellites_visible} SAT)"
        gps_w = cv2.getTextSize(gps_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)[0][0]
        draw_osd_text(img, gps_text, (w - gps_w - 22, bar_y - 14), gps_color, scale=0.40, thickness=1)

    # 5. Center Flight Reticle (Supercar / Aerospace HUD Reticle)
    if show_crosshair:
        cx, cy = w // 2, h // 2
        # Outline
        cv2.line(img, (cx - 16, cy), (cx - 5, cy), (0, 0, 0), 3, cv2.LINE_AA)
        cv2.line(img, (cx + 5, cy), (cx + 16, cy), (0, 0, 0), 3, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 16), (cx, cy - 5), (0, 0, 0), 3, cv2.LINE_AA)
        cv2.line(img, (cx, cy + 5), (cx, cy + 16), (0, 0, 0), 3, cv2.LINE_AA)
        cv2.circle(img, (cx, cy), 3, (0, 0, 0), 3, cv2.LINE_AA)

        # Supercar Bright Gold & Hacker Neon Reticle
        cv2.line(img, (cx - 16, cy), (cx - 5, cy), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
        cv2.line(img, (cx + 5, cy), (cx + 16, cy), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
        cv2.line(img, (cx, cy - 16), (cx, cy - 5), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
        cv2.line(img, (cx, cy + 5), (cx, cy + 16), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
        cv2.circle(img, (cx, cy), 2, COLOR_NEON_GREEN, -1, cv2.LINE_AA)

        # Precision Gold Corner Ticks
        draw_corner_brackets(img, (cx - 30, cy - 30), (cx + 30, cy + 30), color=COLOR_GOLD_DARK, length=6, thickness=1)

    return img


def draw_waiting_screen(counter: int, state: str, reason: str, last_sender: str, telem: Optional[DroneTelemetryState] = None) -> np.ndarray:
    """
    Renders a high-tech animated FPV Ground Station radar screen
    with Hacker Neon + Stealth Pitch Black + Stark White + Bright Gold Supercar Bordering.
    """
    w, h = 800, 540
    frame = np.full((h, w, 3), COLOR_BLACK_PITCH, dtype=np.uint8)

    cx, cy = w // 2, h // 2 - 15

    is_reconnecting = (state == ConnectionState.RECONNECTING)
    primary_color = COLOR_GOLD_BRIGHT if is_reconnecting else COLOR_NEON_GREEN
    status_title = "TRANSMITTER DISCONNECTED" if is_reconnecting else "SEARCHING FOR TRANSMITTER FEED..."

    # Double Supercar Bright Gold Outer Frame
    cv2.rectangle(frame, (10, 10), (w - 10, h - 10), COLOR_GOLD_DARK, 1, cv2.LINE_AA)
    cv2.rectangle(frame, (14, 14), (w - 14, h - 14), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
    draw_corner_brackets(frame, (14, 14), (w - 14, h - 14), color=COLOR_GOLD_BRIGHT, length=24, thickness=2)

    # Top Brand / Livery Banner
    brand_text = "// CYBERPULSE FPV // GROUND TELEMETRY MK-IV"
    (bw, _), _ = cv2.getTextSize(brand_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
    bx1 = (w - bw) // 2 - 14
    bx2 = (w + bw) // 2 + 14
    cv2.rectangle(frame, (bx1, 14), (bx2, 38), COLOR_BLACK_PANEL, -1)
    cv2.rectangle(frame, (bx1, 14), (bx2, 38), COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)
    cv2.putText(frame, brand_text, ((w - bw) // 2, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.40, COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)

    # Animated radar rings (Gold / Carbon grid)
    cv2.circle(frame, (cx, cy), 115, COLOR_GOLD_DARK, 1)
    cv2.circle(frame, (cx, cy), 80, (25, 30, 40), 1)
    cv2.circle(frame, (cx, cy), 45, (25, 30, 40), 1)
    cv2.circle(frame, (cx, cy), 15, COLOR_GOLD_DARK, 1)

    # Cross axis with tick marks
    cv2.line(frame, (cx - 130, cy), (cx + 130, cy), COLOR_GOLD_DARK, 1)
    cv2.line(frame, (cx, cy - 130), (cx, cy + 130), COLOR_GOLD_DARK, 1)
    for tick in [-115, -80, -45, 45, 80, 115]:
        cv2.line(frame, (cx + tick, cy - 4), (cx + tick, cy + 4), COLOR_GOLD_BRIGHT, 1)
        cv2.line(frame, (cx - 4, cy + tick), (cx + 4, cy + tick), COLOR_GOLD_BRIGHT, 1)

    # Animated radar sweep beam (Hacker Neon Green)
    angle = (counter * 5) % 360
    rad = np.deg2rad(angle)
    sx = int(cx + 115 * np.cos(rad))
    sy = int(cy + 115 * np.sin(rad))

    # Trailing glow arc
    for offset in range(1, 14):
        trail_rad = np.deg2rad((angle - offset * 2) % 360)
        tx = int(cx + 115 * np.cos(trail_rad))
        ty = int(cy + 115 * np.sin(trail_rad))
        alpha_color = (int(57 * (1 - offset / 14)), int(255 * (1 - offset / 14)), int(20 * (1 - offset / 14)))
        cv2.line(frame, (cx, cy), (tx, ty), alpha_color, 1, cv2.LINE_AA)

    cv2.line(frame, (cx, cy), (sx, sy), COLOR_NEON_GREEN, 2, cv2.LINE_AA)

    # Pulsing center target dot
    pulse_radius = 4 + int(2 * np.sin(counter * 0.18))
    cv2.circle(frame, (cx, cy), pulse_radius, COLOR_NEON_GREEN, -1, cv2.LINE_AA)

    # Radar Blips (Simulated drone signals)
    blip_a_x, blip_a_y = cx + 55, cy - 45
    cv2.circle(frame, (blip_a_x, blip_a_y), 3, COLOR_GOLD_BRIGHT, -1, cv2.LINE_AA)
    cv2.circle(frame, (blip_a_x, blip_a_y), 6, COLOR_GOLD_DARK, 1, cv2.LINE_AA)

    # Status Banner Box
    (tw, _), _ = cv2.getTextSize(status_title, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
    banner_w = tw + 40
    draw_hud_box(frame, ((w - banner_w) // 2, cy - 170), ((w + banner_w) // 2, cy - 135), border_color=COLOR_GOLD_BRIGHT, fill_alpha=0.7)
    cv2.putText(frame, status_title, ((w - tw) // 2, cy - 146), cv2.FONT_HERSHEY_SIMPLEX, 0.65, primary_color, 2, cv2.LINE_AA)

    # Informational Cards
    if is_reconnecting:
        sub_text = "AUTO-RECONNECTING... LISTENING ON UDP PORT 5005"
        (sw, _), _ = cv2.getTextSize(sub_text, cv2.FONT_HERSHEY_SIMPLEX, 0.44, 1)
        cv2.putText(frame, sub_text, (cx - sw // 2, cy + 148), cv2.FONT_HERSHEY_SIMPLEX, 0.44, COLOR_WHITE_PURE, 1, cv2.LINE_AA)

        if reason:
            reason_text = f"Notice: {reason}"
            (rw, _), _ = cv2.getTextSize(reason_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
            cv2.putText(frame, reason_text, (cx - rw // 2, cy + 172), cv2.FONT_HERSHEY_SIMPLEX, 0.40, COLOR_TEXT_MUTED, 1, cv2.LINE_AA)

        if last_sender and last_sender != "Unknown":
            sender_text = f"Last Transmitter: {last_sender}"
            (sw2, _), _ = cv2.getTextSize(sender_text, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
            cv2.putText(frame, sender_text, (cx - sw2 // 2, cy + 196), cv2.FONT_HERSHEY_SIMPLEX, 0.38, COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)

        hint_text = "FEED WILL AUTOMATICALLY RESUME AS SOON AS THE TRANSMITTER STARTS"
        (hw, _), _ = cv2.getTextSize(hint_text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
        cv2.putText(frame, hint_text, (cx - hw // 2, cy + 224), cv2.FONT_HERSHEY_SIMPLEX, 0.42, COLOR_NEON_GREEN, 1, cv2.LINE_AA)
    else:
        laptop_ip = detect_laptop_ipv6() or "SEARCHING"
        sub_text = f"LISTENING ON UDP PORT 5005  |  GROUND IP: [{laptop_ip}]"
        (sw, _), _ = cv2.getTextSize(sub_text, cv2.FONT_HERSHEY_SIMPLEX, 0.40, 1)
        cv2.putText(frame, sub_text, (cx - sw // 2, cy + 148), cv2.FONT_HERSHEY_SIMPLEX, 0.40, COLOR_GOLD_BRIGHT, 1, cv2.LINE_AA)

        if telem and telem.is_heartbeat_fresh:
            armed_str = "ARMED" if telem.armed else "DISARMED"
            telem_banner = f"MAVLINK ACTIVE: [{armed_str}] {telem.flight_mode}  |  BAT: {telem.battery_voltage:.1f}V  |  {telem.gps_fix_str}"
            (tbw, _), _ = cv2.getTextSize(telem_banner, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.putText(frame, telem_banner, (cx - tbw // 2, cy + 176), cv2.FONT_HERSHEY_SIMPLEX, 0.42, COLOR_NEON_GREEN, 1, cv2.LINE_AA)
        else:
            hint_text = "VERCEL SYNCED • ENGAGE TRANSMITTER ON ANDROID TO START STREAM"
            (hw, _), _ = cv2.getTextSize(hint_text, cv2.FONT_HERSHEY_SIMPLEX, 0.42, 1)
            cv2.putText(frame, hint_text, (cx - hw // 2, cy + 176), cv2.FONT_HERSHEY_SIMPLEX, 0.42, COLOR_NEON_GREEN, 1, cv2.LINE_AA)

    # Footer Supercar Keybindings Bar
    footer_text = "KEYS: [Q] QUIT  |  [F] FULLSCREEN  |  [H] TOGGLE HUD  |  [C] CROSSHAIR"
    (fw, _), _ = cv2.getTextSize(footer_text, cv2.FONT_HERSHEY_SIMPLEX, 0.38, 1)
    cv2.rectangle(frame, ((w - fw) // 2 - 12, h - 34), ((w + fw) // 2 + 12, h - 16), COLOR_BLACK_PANEL, -1)
    cv2.rectangle(frame, ((w - fw) // 2 - 12, h - 34), ((w + fw) // 2 + 12, h - 16), COLOR_GOLD_DARK, 1, cv2.LINE_AA)
    cv2.putText(frame, footer_text, ((w - fw) // 2, h - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.38, COLOR_WHITE_PURE, 1, cv2.LINE_AA)

    return frame


def main():
    print("=" * 65, flush=True)
    print(" Drone FPV Ground Control  |  ESP32 Bluetooth Transport", flush=True)
    print(" Video Feed UDP Listener Port:     5005", flush=True)
    print(" Telemetry BT-Serial Ingress Port: 14551 (via bt_bridge)", flush=True)
    print(" Mission Planner Gateway:          UDP 127.0.0.1:14550", flush=True)
    print(" Mission Planner TCP Fallback:     TCP 127.0.0.1:5760", flush=True)
    print("=" * 65, flush=True)

    # Set Windows kernel timer resolution to 1ms to eliminate cv2.waitKey 15.6ms jitter
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass

    # Initialize Telemetry Router
    telem_router = TelemetryRouter(
        phone_bind_ip="0.0.0.0",
        phone_bind_port=14551,
        mp_host="127.0.0.1",
        mp_port=14550,
        enable_tcp=True,
        on_log=lambda m: print(f"[TELEM] {m}", flush=True),
    )
    telem_router.start()

    def on_peer_update_callback(peer_ip: str, peer_port: int):
        print(f"[REGISTRY] Live peer updated -> [{peer_ip}]:{peer_port}", flush=True)

    # Zero-touch Vercel Discovery Registry Daemon:
    # Registers GROUND-001 with dynamic IPv6, checks local interface every 750ms internally,
    # and automatically receives peer updates from Vercel.
    registry_client = GroundStationRegistryClient(
        port=5005,
        on_peer_update=on_peer_update_callback,
    )
    registry_client.start_background_daemon()

    latest_frame = [None]
    latest_stats = [None]
    is_fullscreen = False
    show_hud = True
    show_crosshair = True

    # 2-Second Moving Average Latency Tracker (pure rolling window, zero stickiness)
    latency_samples = collections.deque()
    min_observed_diff = [None]
    min_observed_time = [0.0]
    avg_latency_2s = [0.0]
    last_feedback_time = [0.0]
    new_frame_event = threading.Event()

    def record_latency(sender_ts_ms: int):
        if sender_ts_ms <= 0:
            return
        now_perf = time.time()
        now_ms = int(now_perf * 1000)
        raw_diff = float(now_ms - sender_ts_ms)

        # Baseline calibration (tracks minimum physical transit + clock offset)
        # Smoothly adapts to slow clock drift without harsh 60s spike resets
        if min_observed_diff[0] is None:
            min_observed_diff[0] = raw_diff
            min_observed_time[0] = now_perf
        elif raw_diff < min_observed_diff[0]:
            min_observed_diff[0] = raw_diff
            min_observed_time[0] = now_perf
        elif now_perf - min_observed_time[0] > 10.0:
            # Gentle drift tracking: relax baseline minimum upward by 1ms every 10s
            min_observed_diff[0] += 1.0
            min_observed_time[0] = now_perf

        # Estimated one-way latency: baseline (~18ms at 60 FPS, ~38ms at 30 FPS) + transit / queuing delay
        base_hardware_ms = 18.0 if (latest_stats[0] and latest_stats[0].fps > 45.0) else 38.0
        instant_lat = max(10.0, base_hardware_ms + (raw_diff - min_observed_diff[0]))

        # Add to rolling window
        latency_samples.append((now_perf, instant_lat))

        # Evict samples older than 2.0 seconds
        cutoff = now_perf - 2.0
        while latency_samples and latency_samples[0][0] < cutoff:
            latency_samples.popleft()

    def get_2s_avg_latency() -> float:
        now_perf = time.time()
        cutoff = now_perf - 2.0
        while latency_samples and latency_samples[0][0] < cutoff:
            latency_samples.popleft()
        if not latency_samples:
            return 0.0
        return sum(l for _, l in latency_samples) / len(latency_samples)

    def on_frame_callback(frame: np.ndarray, stats: FrameStats):
        latest_frame[0] = frame
        latest_stats[0] = stats
        if stats.sender_timestamp_ms > 0:
            record_latency(stats.sender_timestamp_ms)
        new_frame_event.set()

    def on_connection_callback(connected: bool, reason: str, sender_str: Optional[str]):
        if not connected:
            # Transmitter stopped or signal lost: reset frame display and calibration
            latest_frame[0] = None
            latest_stats[0] = None
            min_observed_diff[0] = None
            latency_samples.clear()
            avg_latency_2s[0] = 0.0
        else:
            # Fresh connection established: ready for new stream calibration
            min_observed_diff[0] = None
            latency_samples.clear()
            avg_latency_2s[0] = 0.0
        new_frame_event.set()

    receiver = ZeroLatencyVideoReceiver(
        bind_ip="0.0.0.0",
        port=5005,
        on_frame=on_frame_callback,
        on_connection_change=on_connection_callback,
        on_log=lambda msg: print(msg, flush=True),
        on_peer_update=on_peer_update_callback,
    )
    receiver.start()

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 960, 720)

    counter = 0

    try:
        while True:
            # Check if user clicked window close 'X' button
            if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
                break

            # Event-driven sync: wait up to 16ms for fresh frame, freeing Python GIL for socket ingest
            new_frame_event.wait(timeout=0.016)
            new_frame_event.clear()

            is_connected = receiver.is_connected
            frame = latest_frame[0]
            stats = latest_stats[0]

            if is_connected and frame is not None and stats is not None:
                now_perf = time.time()
                avg_latency_2s[0] = get_2s_avg_latency()

                if now_perf - last_feedback_time[0] >= 0.1:
                    last_feedback_time[0] = now_perf
                    receiver.send_feedback(avg_latency_2s[0])

                telem_state = telem_router.get_state()

                if show_hud:
                    display_frame = draw_hud(
                        frame.copy(),
                        stats,
                        avg_latency_2s[0],
                        sender_str=receiver.last_sender_str,
                        telem=telem_state,
                        show_crosshair=show_crosshair,
                    )
                elif show_crosshair:
                    display_frame = frame.copy()
                    h, w = display_frame.shape[:2]
                    cx, cy = w // 2, h // 2
                    cv2.line(display_frame, (cx - 14, cy), (cx - 4, cy), (56, 189, 248), 1, cv2.LINE_AA)
                    cv2.line(display_frame, (cx + 4, cy), (cx + 14, cy), (56, 189, 248), 1, cv2.LINE_AA)
                    cv2.line(display_frame, (cx, cy - 14), (cx, cy - 4), (56, 189, 248), 1, cv2.LINE_AA)
                    cv2.line(display_frame, (cx, cy + 4), (cx, cy + 14), (56, 189, 248), 1, cv2.LINE_AA)
                    cv2.circle(display_frame, (cx, cy), 1, (56, 189, 248), -1)
                else:
                    display_frame = frame
            else:
                # Rule 2: If targeted IP is not responding for more than 10 seconds, only then look up
                now_perf = time.time()
                last_pkt = receiver.last_packet_time
                if last_pkt == 0.0 or (now_perf - last_pkt > 10.0):
                    drone_data = registry_client.lookup_if_unresponsive(
                        drone_id="DRONE-001",
                        last_response_time=last_pkt,
                        unresponsive_threshold_s=10.0,
                    )
                    if drone_data and drone_data.get("ipv6"):
                        discovered_ip = drone_data.get("ipv6")
                        discovered_port = drone_data.get("port", 5005)
                        print(f"[REGISTRY] Discovered Drone at [{discovered_ip}]:{discovered_port}", flush=True)

                display_frame = draw_waiting_screen(
                    counter=counter,
                    state=receiver.connection_state,
                    reason=receiver.disconnect_reason,
                    last_sender=receiver.last_sender_str,
                    telem=telem_router.get_state(),
                )

            cv2.imshow(WINDOW_NAME, display_frame)

            counter += 1
            key = cv2.waitKey(1) & 0xFF

            if key == ord("q") or key == 27:  # 'q' or ESC
                break
            elif key == ord("f"):  # Toggle fullscreen
                is_fullscreen = not is_fullscreen
                prop = cv2.WINDOW_FULLSCREEN if is_fullscreen else cv2.WINDOW_NORMAL
                cv2.setWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN, prop)
            elif key == ord("h"):  # Toggle HUD OSD
                show_hud = not show_hud
            elif key == ord("c"):  # Toggle center flight crosshair
                show_crosshair = not show_crosshair

    finally:
        print("\nStopping receiver, telemetry router, and registry daemon...", flush=True)
        registry_client.stop()
        receiver.stop()
        telem_router.stop()
        cv2.destroyAllWindows()
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.winmm.timeEndPeriod(1)
            except Exception:
                pass
        print("Done. Clean exit.", flush=True)


if __name__ == "__main__":
    main()

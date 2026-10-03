# Drone Control v4

Ultra-Low Latency Drone FPV Video Streaming & Bi-Directional MAVLink Telemetry System with Supercar Cockpit UI (Hacker Neon + Pitch Black + Stark White + Bright Gold Livery).

---

## Architecture Overview

```
 [ Drone / Flight Controller ]
           │ (Serial / UART)
           ▼
     [ ESP32 Bridge ]
           │ (Bluetooth SPP: "APM-Bridge")
           ▼
 [ Android Transmitter App ] ─── (Camera Feed: H.264 UDP 5005) ───► [ Ground Control Station ]
 (Headless Qualcomm AVC Pipe) ── (MAVLink Relay: UDP 14551)   ───► (Python OpenCV HUD & MAVLink Router)
                                                                           │
                                                                           ├──► Mission Planner (UDP 14550)
                                                                           └──► Mission Planner (TCP 5760)
```

---

## Key Features

- **Ultra-Low Latency Video Streaming**:
  - Direct hardware-accelerated Qualcomm AVC/H.264 encoder on Android.
  - Headless streaming engine bypasses GPU preview composition, minimizing glass-to-glass latency (<100ms) and power consumption.
  - Zero-latency Python UDP receiver with fast non-blocking frame ingestion and multi-threaded OpenCV decoding.

- **Bi-Directional Telemetry Bridge**:
  - Ingests MAVLink telemetry from flight controller over ESP32 Bluetooth Serial (`APM-Bridge`).
  - Multiplexes telemetry over UDP port 14551 to ground station.
  - Routes MAVLink seamlessly to Mission Planner / QGroundControl via UDP (`127.0.0.1:14550`) and TCP (`127.0.0.1:5760`).

- **Redesigned Supercar Cockpit UI (v4)**:
  - **Color Palette**: Pitch Black (`#050508`), Bright Gold (`#FFD700`) automotive borders, Hacker Neon Green (`#00FF66` / `#39FF14`), and Stark Pure White (`#FFFFFF`).
  - **Android Transmitter**:
    - MaterialCardView panels with `1.5dp` bright gold livery bordering.
    - Supercar 2×3 cockpit instrument matrix (Bitrate, FPS, Packets, Telemetry Uplink/Downlink, Link Status).
    - Launch control ignition toggle button (`ENGAGE // START STREAMING`).
  - **Ground Station HUD**:
    - Glass-to-glass latency & bitrate monitor with gold corner brackets.
    - High-visibility OSD overlay with live flight telemetry (Altitude, Speed, Heading, GPS 3D Fix, Battery Voltage).
    - Precision targeting reticle with hacker neon crosshairs and gold brackets.
    - Animated high-tech radar waiting screen with rotating neon sweep beam and target blips.

---

## Directory Structure

```
├── android-transmitter/    # Android camera streamer & Bluetooth MAVLink bridge
│   ├── app/                # Kotlin application source & resources
│   └── build.gradle.kts    # Gradle build config
├── ground-control/         # Python FPV receiver and telemetry router
│   ├── src/                # Python receiver, router, and HUD
│   │   ├── main.py         # Main FPV HUD application & radar screen
│   │   ├── receiver.py     # High-speed UDP H.264 frame receiver
│   │   ├── telemetry_router.py  # MAVLink telemetry router & Mission Planner gateway
│   │   └── bt_bridge.py    # Direct Bluetooth serial bridge fallback
│   ├── config/             # Config JSON
│   └── requirements.txt    # Python dependencies
├── esp_diagnostic/         # PlatformIO ESP32 diagnostic & firmware bridge
├── start_receiver.bat      # One-click Windows ground station launcher
├── adb_vpn_connect.bat     # ADB network connect helper script
└── .gitignore
```

---

## Quick Start

### 1. Android Transmitter
1. Open `android-transmitter` in Android Studio or compile with:
   ```bash
   cd android-transmitter
   ./gradlew assembleDebug
   ```
2. Install APK onto the smartphone mounted on the drone.
3. Configure Target Laptop IP and tap **`ENGAGE // START STREAMING`**.

### 2. Ground Station (Laptop / PC)
1. Install Python dependencies:
   ```bash
   cd ground-control
   pip install -r requirements.txt
   ```
2. Launch the Ground Control Station:
   ```bash
   # From root:
   start_receiver.bat
   
   # Or directly:
   python ground-control/src/main.py
   ```
3. Controls:
   - `[Q]` : Quit
   - `[F]` : Toggle Fullscreen
   - `[H]` : Toggle HUD Elements
   - `[C]` : Toggle Flight Reticle

---

## License

MIT License

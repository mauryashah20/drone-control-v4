# Android Video Transmitter

Android application for capturing camera video, encoding to H.264, streaming over UDP, and transmitting GPS/sensor data.

## Features

- **Camera2 API**: High-quality video capture with configurable resolution
- **H.264 Encoding**: Hardware-accelerated MediaCodec encoding
- **UDP Streaming**: Low-latency video transmission
- **GPS Integration**: Real-time location tracking
- **Magnetometer**: Accurate heading/compass data
- **Quality Presets**: 5 quality levels (Very Low to High)
- **FPS Control**: Configurable frame rates (15/20/30 FPS)
- **Remote Control**: Receive quality/FPS commands from ground station
- **Joystick Relay**: Forward joystick data to ESP32 via Bluetooth
- **Flash Control**: Toggle camera torch remotely

## Requirements

- Android 8.0 (API 26) or higher
- Device with camera and GPS
- Bluetooth for servo control (optional)

## Installation

### Build from Source

1. Open project in Android Studio
2. Sync Gradle dependencies
3. Connect Android device via USB
4. Click "Run" or build APK

### Install APK

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

## Configuration

### First Launch Setup

1. **Grant Permissions**: Allow camera and location access
2. **Configure IP**: Long-press the status text to enter laptop/ground control IP address
3. **Bluetooth** (optional): Pair with ESP32 device named "ESP32_Servo"

### Network Configuration

The app will prompt for the ground control station IP on first launch. You can reconfigure at any time by long-pressing the status text.

**Ports Used**:
- 5005: Video stream output
- 5006: Control commands (receive)
- 5007: GPS/sensor data output

## Usage

### Starting Stream

1. Toggle "Stream" switch to ON
2. App will start streaming video and GPS data to configured IP
3. Quality and FPS can be controlled remotely from ground station

### Bluetooth Servo Control

1. Click "Connect to ESP32" button
2. App will connect to paired ESP32 device
3. Joystick commands from ground station will be forwarded to ESP32

### Quality Presets

| Level | Resolution | Quality | Bitrate |
|-------|-----------|---------|---------|
| 0 - Very Low | 160x120 | 10 | 50 kbps |
| 1 - Low | 240x180 | 12 | 150 kbps |
| 2 - Low-Med | 320x240 | 15 | 300 kbps |
| 3 - Med- | 480x360 | 20 | 600 kbps |
| 4 - High | 640x480 | 24 | 1200 kbps |

### FPS Options

- 15 FPS: Low bandwidth
- 20 FPS: Balanced
- 30 FPS: Smooth (higher bandwidth)

## Architecture

```
app/
├── src/main/
│   ├── java/com/example/udpandroidapptransmitter/
│   │   ├── MainActivity.kt           # Main UI and control logic
│   │   ├── StreamingService.kt       # Background streaming service
│   │   └── SensorManager.kt          # GPS and magnetometer handling
│   ├── res/
│   │   ├── layout/
│   │   │   └── activity_main.xml     # UI layout
│   │   └── values/
│   │       └── strings.xml
│   └── AndroidManifest.xml
└── build.gradle.kts
```

## Troubleshooting

### Video Not Streaming
- Check ground control IP is correct
- Verify both devices on same network/VPN
- Ensure firewall allows UDP port 5005

### GPS Not Working
- Enable location services on device
- Grant location permissions to app
- Move to area with clear sky view

### Bluetooth Connection Failed
- Ensure ESP32 is powered and in range
- Verify ESP32 is paired in Android Bluetooth settings
- Check ESP32 device name is "ESP32_Servo"

### Poor Video Quality
- Increase quality preset (may increase latency)
- Check network bandwidth
- Reduce FPS if network is congested

## Development

### Building

```bash
./gradlew assembleDebug
```

### Running Tests

```bash
./gradlew test
```

## License

See root LICENSE file

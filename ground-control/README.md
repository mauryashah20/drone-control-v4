# Ground Control Station

Desktop application for receiving and displaying real-time video stream, GPS telemetry, and controlling the drone.

## Features

- **Real-time Video Display**: H.264 video stream with FPS counter and latency monitoring
- **GPS Dashboard**: Live coordinates, altitude, speed, heading, and satellite count
- **Interactive Satellite Map**: Google Maps-like interface with drone tracking, follow mode, and accuracy visualization
- **Joystick Control**: USB gamepad support for servo control (4 axes)
- **Remote Configuration**: Adjust video quality, FPS, and camera flash from ground station
- **Network Monitoring**: Ping/pong RTT measurement for latency tracking

## Requirements

- Python 3.8 or higher
- PyQt5
- pygame (for joystick support)
- numpy
- requests

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Running the Application

```bash
python src/main.py
```

Or use the provided script:

**Windows:**
```bash
scripts\run.bat
```

**Linux/Mac:**
```bash
chmod +x scripts/run.sh
./scripts/run.sh
```

### First-Time Setup

1. **Configure Network**: On first launch, you'll be prompted to enter the Android device IP address
2. **Connect Joystick** (optional): Plug in USB gamepad before starting
3. **Start Receiving**: Click "Start Receiving" to begin video stream

### Controls

- **Start/Stop Receiving**: Begin/end video reception
- **Quality Slider**: Adjust transmitter video quality (Very Low to High)
- **FPS Slider**: Set transmitter frame rate (15/20/30 FPS)
- **Flash Button**: Toggle Android camera torch
- **Map Controls**:
  - Zoom +/- : Zoom in/out on map
  - Center: Center map on drone position
  - Set My Location: Manually configure ground control location
  - Follow: Toggle auto-follow mode for drone
  - North: Reset map orientation to north
  - Accuracy: Show/hide GPS accuracy circle

### Joystick Mapping

- **Axis 0**: Servo 0 (mapped to 0-180°)
- **Axis 1**: Servo 1 (mapped to 0-180°)
- **Axis 2**: Servo 2 (mapped to 0-180°)
- **Axis 5**: Servo 3 (mapped to 0-180°)

## Configuration

Edit `config/config.json` to change network settings:

```json
{
  "network": {
    "remote_ip": "192.168.1.100",
    "video_port": 5005,
    "control_port": 5006,
    "gps_port": 5007
  }
}
```

You can also configure the remote IP at runtime by using the network configuration dialog (shown on first launch or accessible via long-press on status text).

## Network Ports

| Port | Protocol | Purpose |
|------|----------|---------|
| 5005 | UDP | Video stream (receive) |
| 5006 | UDP | Control commands (bidirectional) |
| 5007 | UDP | GPS/sensor data (receive) |

## Troubleshooting

### No Video Stream
- Verify Android device IP is correct in config
- Check firewall allows UDP ports 5005-5007
- Ensure both devices are on same network (or VPN)

### Joystick Not Detected
- Ensure pygame is installed: `pip install pygame`
- Plug in joystick before starting application
- Check joystick is recognized by OS

### GPS Not Updating
- Verify Android app has location permissions
- Check GPS is enabled on Android device
- Ensure port 5007 is not blocked

### High Latency
- Check network connection quality
- Reduce video quality/FPS settings
- Use wired connection if possible
- Verify no other applications using bandwidth

## Scripts

- `scripts/run.bat` - Windows launcher
- `scripts/test_magnetometer_sender.py` - Test script for magnetometer data

## Documentation

- [Enhanced Map Features](docs/enhanced-map-features.md)
- [Magnetometer Integration](docs/magnetometer-integration.md)

## Architecture

```
ground-control/
├── src/
│   ├── main.py                    # Main application entry point
│   ├── receiver.py                # UDP video receiver
│   └── satellite_map_features.py  # Map widget and features
├── config/
│   └── config.json                # Network configuration
├── scripts/
│   ├── run.bat                    # Windows launcher
│   └── test_magnetometer_sender.py
└── docs/
    ├── enhanced-map-features.md
    └── magnetometer-integration.md
```

## License

See root LICENSE file

# Magnetometer Integration for Enhanced Direction Tracking

## Overview

This document describes the integration of the phone's onboard magnetometer for more accurate and responsive direction tracking in the satellite map. The magnetometer provides real-time heading data that is more precise and responsive than GPS bearing, especially when the device is stationary or moving slowly.

## Features Added

### 🧭 **Magnetometer Support**
- **Real-time heading data** from phone's magnetometer
- **Higher accuracy** than GPS bearing, especially when stationary
- **Faster response** to direction changes
- **Toggle between sources** - magnetometer vs GPS bearing

### 📡 **Enhanced Data Reception**
- **Multiple data formats** supported:
  - `gps:` - GPS data only
  - `mag:` - Magnetometer data only  
  - `sensor:` - Combined GPS + magnetometer data
- **Magnetometer-only heading** - System exclusively uses magnetometer data
- **Clear indication** when magnetometer data is not available

### 🎛️ **System Behavior**
- **Magnetometer-only heading** - System hardwired to use magnetometer data exclusively
- **Real-time source indicator** in heading display
- **Debug logging** showing magnetometer data reception and values
- **Fallback indication** when magnetometer data is not available

## Data Formats

### GPS Data Format
```
gps:lat,lon,alt,acc,speed,bearing
```
Example: `gps:40.7128,-74.0060,10.5,3.2,5.1,45.0`

### Magnetometer Data Format
```
mag:heading,accuracy,x,y,z
```
Example: `mag:45.2,2.1,0.707,0.707,0.0`

### Combined Sensor Data Format
```
sensor:lat,lon,alt,acc,speed,gps_bearing,mag_heading,mag_acc,x,y,z
```
Example: `sensor:40.7128,-74.0060,10.5,3.2,5.1,45.0,45.2,2.1,0.707,0.707,0.0`

## Android Implementation

To send magnetometer data from your Android device, you'll need to implement sensor listeners. Here's a basic example:

### Java/Kotlin Code for Android

```java
// Sensor Manager Setup
SensorManager sensorManager = (SensorManager) getSystemService(Context.SENSOR_SERVICE);
Sensor magnetometer = sensorManager.getDefaultSensor(Sensor.TYPE_MAGNETIC_FIELD);
Sensor accelerometer = sensorManager.getDefaultSensor(Sensor.TYPE_ACCELEROMETER);

// Sensor Event Listener
private SensorEventListener sensorEventListener = new SensorEventListener() {
    private float[] lastAccelerometer = new float[3];
    private float[] lastMagnetometer = new float[3];
    private boolean lastAccelerometerSet = false;
    private boolean lastMagnetometerSet = false;
    private float[] rotationMatrix = new float[9];
    private float[] orientation = new float[3];

    @Override
    public void onSensorChanged(SensorEvent event) {
        if (event.sensor == accelerometer) {
            System.arraycopy(event.values, 0, lastAccelerometer, 0, event.values.length);
            lastAccelerometerSet = true;
        } else if (event.sensor == magnetometer) {
            System.arraycopy(event.values, 0, lastMagnetometer, 0, event.values.length);
            lastMagnetometerSet = true;
        }

        if (lastAccelerometerSet && lastMagnetometerSet) {
            SensorManager.getRotationMatrix(rotationMatrix, null, lastAccelerometer, lastMagnetometer);
            SensorManager.getOrientation(rotationMatrix, orientation);
            
            float azimuth = orientation[0] * 180 / (float) Math.PI;
            if (azimuth < 0) azimuth += 360;
            
            // Send magnetometer data
            sendMagnetometerData(azimuth, event.values[0], event.values[1], event.values[2]);
        }
    }

    @Override
    public void onAccuracyChanged(Sensor sensor, int accuracy) {
        // Handle accuracy changes
    }
};

// Send magnetometer data via UDP
private void sendMagnetometerData(float heading, float x, float y, float z) {
    String message = String.format("mag:%.1f,%.1f,%.3f,%.3f,%.3f", 
                                  heading, 2.0f, x, y, z);
    sendUDPData(message, 5007); // Same port as GPS data
}

// Send combined sensor data
private void sendCombinedSensorData(double lat, double lon, double alt, 
                                   float gpsAcc, float speed, float gpsBearing,
                                   float magHeading, float magAcc, 
                                   float magX, float magY, float magZ) {
    String message = String.format("sensor:%.6f,%.6f,%.1f,%.1f,%.1f,%.1f,%.1f,%.1f,%.3f,%.3f,%.3f",
                                  lat, lon, alt, gpsAcc, speed, gpsBearing, 
                                  magHeading, magAcc, magX, magY, magZ);
    sendUDPData(message, 5007);
}
```

### Python UDP Sender (for testing)

```python
import socket
import time
import math

def send_test_magnetometer_data():
    """Send test magnetometer data for development"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    # Test data - simulating rotation
    for angle in range(0, 360, 10):
        heading = angle
        x = math.cos(math.radians(angle))
        y = math.sin(math.radians(angle))
        z = 0.0
        
        message = f"mag:{heading:.1f},2.0,{x:.3f},{y:.3f},{z:.3f}"
        sock.sendto(message.encode(), ('192.168.191.187', 5007))
        print(f"Sent: {message}")
        time.sleep(0.5)
    
    sock.close()
```

## Usage

### Magnetometer-Only Mode
The system is hardwired to use magnetometer data exclusively for heading. When magnetometer data is not available, the system shows "Waiting for Magnetometer" status.

### Data Priority
1. **Magnetometer** (exclusive source for heading)
2. **Waiting State** (when magnetometer data unavailable)

## Benefits

### 🎯 **Accuracy**
- **Magnetometer**: ±1-3° accuracy, works when stationary
- **GPS Bearing**: ±5-10° accuracy, requires movement

### ⚡ **Responsiveness**
- **Magnetometer**: Instant response to direction changes
- **GPS Bearing**: Delayed response, requires movement

### 🔋 **Battery Efficiency**
- **Magnetometer**: Low power consumption
- **GPS Bearing**: Higher power consumption

## Troubleshooting

### Common Issues

1. **No magnetometer data received**
   - Check Android sensor permissions
   - Verify UDP port 5007 is accessible
   - Check sensor availability on device

2. **Inaccurate heading**
   - Calibrate magnetometer on Android device
   - Avoid magnetic interference (metal objects, electronics)
   - Check for device case interference

3. **Heading not updating**
   - Verify sensor event listeners are active
   - Check data format matches expected format
   - Review debug logs for data reception

### Debug Information

The application logs the following information:
- GPS data reception: `"Received GPS: lat, lon, GPS Bearing: X.X°"`
- Magnetometer data: `"Received Magnetometer: Heading: X.X°, Accuracy: X.X°"`
- Combined data: `"Received Combined: GPS(lat, lon), GPS Bearing: X.X°, Mag Heading: X.X°"`
- Map updates: `"Updated drone marker: Lat=X, Lon=X, Heading=X.X° (Source)"`

## Testing

### Test Magnetometer Integration
```bash
python test_magnetometer.py
```

### Manual Testing
1. Start the application
2. Send magnetometer data from Android device
3. Verify heading updates in real-time using magnetometer data
4. Check debug logs for magnetometer data reception
5. Verify "Waiting for Magnetometer" status when no magnetometer data

## Future Enhancements

Potential improvements:
- **Sensor fusion** - Combine magnetometer + accelerometer + gyroscope
- **Calibration tools** - Built-in magnetometer calibration
- **Interference detection** - Detect and warn about magnetic interference
- **Historical data** - Track heading accuracy over time
- **Custom calibration** - User-defined magnetic declination

## Technical Details

### Coordinate System
- **Magnetometer heading**: 0° = North, 90° = East, 180° = South, 270° = West
- **GPS bearing**: Same coordinate system
- **Raw magnetometer values**: Device coordinate system (x, y, z)

### Data Processing
- **Heading calculation**: Uses rotation matrix from accelerometer + magnetometer
- **Accuracy estimation**: Based on sensor accuracy and magnetic field strength
- **Smoothing**: Optional low-pass filtering for stable readings

---

**Note**: Magnetometer accuracy can be affected by magnetic interference from nearby objects, electronic devices, or the device case. For best results, calibrate the magnetometer regularly and avoid magnetic interference sources.

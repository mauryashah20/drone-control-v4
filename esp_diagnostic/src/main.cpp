/**
 * APM 2.8 → ESP32 Bluetooth MAVLink Bridge
 * ==========================================
 * Replaces the old ESP8266 WiFi diagnostic.
 *
 * WHAT IT DOES:
 *   • Exposes a Bluetooth Classic SPP device named "APM-Bridge"
 *   • Bridges MAVLink between APM 2.8 TELEM UART and the BT SPP channel
 *   • APM TX  → ESP32 RX → Bluetooth → Android phone / Mission Planner (via BT)
 *   • Android / BT GCS → Bluetooth → ESP32 TX → APM RX
 *   • Periodically sends GCS heartbeat + REQUEST_DATA_STREAM to wake APM
 *   • Tiny health page over Serial Monitor (USB) at 115200 for diagnostics
 *
 * HARDWARE WIRING  (see connection guide at the bottom of this file)
 *   APM TELEM1 TX  → ESP32 GPIO 16 (UART2 RX)
 *   APM TELEM1 RX  → ESP32 GPIO 17 (UART2 TX)   [via 5V→3.3V divider]
 *   APM GND        → ESP32 GND
 *   APM 5V         → ESP32 VIN  (or power ESP32 separately via USB)
 *
 * BAUD RATES:
 *   APM TELEM1 default = 57600 bps  → change APM parameter SERIAL1_BAUD if needed
 *   USB Serial Monitor  = 115200 bps (diagnostic output only)
 */

#include <Arduino.h>
#include <BluetoothSerial.h>

// ──────────────────────────────────────────────────────────────────────────────
//  Configuration
// ──────────────────────────────────────────────────────────────────────────────
#define APM_UART_RX        16          // ESP32 GPIO connected to APM TELEM TX
#define APM_UART_TX        17          // ESP32 GPIO connected to APM TELEM RX
#define APM_BAUD           57600       // APM 2.8 TELEM1 default baud rate
#define BT_DEVICE_NAME     "APM-Bridge"

#define STREAM_REQUEST_INTERVAL_MS  2000   // Send heartbeat + stream req every 2s
#define STATUS_PRINT_INTERVAL_MS    5000   // Print stats to USB Serial every 5s
#define UART_BUF_SIZE               512    // UART read chunk size

// ──────────────────────────────────────────────────────────────────────────────
//  Pre-computed MAVLink 1 packets  (System 255, Component 190 = GCS)
// ──────────────────────────────────────────────────────────────────────────────

// HEARTBEAT  (msg id 0x00, type 6=GCS, autopilot 8=invalid, base_mode 0)
static const uint8_t MAV_HEARTBEAT[] = {
    0xFE, 0x09, 0x00, 0xFF, 0xBE, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x06, 0x08, 0x00, 0x04, 0x03,
    0x49, 0x21
};

// REQUEST_DATA_STREAM ALL @ 4 Hz  (target sys 1, comp 1, stream 0, rate 4, start 1)
static const uint8_t MAV_REQUEST_STREAM[] = {
    0xFE, 0x06, 0x00, 0xFF, 0xBE, 0x42,
    0x04, 0x00, 0x01, 0x01, 0x00, 0x01,
    0x97, 0x72
};

// ──────────────────────────────────────────────────────────────────────────────
//  Globals
// ──────────────────────────────────────────────────────────────────────────────
BluetoothSerial SerialBT;

// Counters for health display
volatile uint32_t g_apm_bytes_rx    = 0;  // bytes received FROM APM
volatile uint32_t g_apm_bytes_tx    = 0;  // bytes sent TO APM
volatile uint32_t g_bt_bytes_rx     = 0;  // bytes received FROM BT client
volatile uint32_t g_bt_bytes_tx     = 0;  // bytes sent TO BT client
volatile uint32_t g_mavlink_frames  = 0;  // MAVLink start-byte count

bool    g_bt_was_connected = false;
uint32_t g_last_stream_req = 0;
uint32_t g_last_status_print = 0;

// Last 32 raw bytes from APM (sliding window) for hex dump
uint8_t  g_raw_buf[32];
uint8_t  g_raw_pos = 0;
bool     g_raw_full = false;

// ──────────────────────────────────────────────────────────────────────────────
//  Helpers
// ──────────────────────────────────────────────────────────────────────────────
void recordRaw(uint8_t b) {
    // Count MAVLink frame starts
    if (b == 0xFE || b == 0xFD) g_mavlink_frames++;
    g_raw_buf[g_raw_pos] = b;
    g_raw_pos = (g_raw_pos + 1) & 0x1F;  // wrap at 32
    if (g_raw_pos == 0) g_raw_full = true;
}

void sendApmWakeup() {
    Serial2.write(MAV_HEARTBEAT, sizeof(MAV_HEARTBEAT));
    Serial2.write(MAV_REQUEST_STREAM, sizeof(MAV_REQUEST_STREAM));
    g_apm_bytes_tx += sizeof(MAV_HEARTBEAT) + sizeof(MAV_REQUEST_STREAM);
    Serial.println("[WAKEUP] Sent GCS heartbeat + REQUEST_DATA_STREAM to APM");
}

void printStatus() {
    bool bt_conn = SerialBT.connected();
    Serial.println("─────────────────────────────────────────");
    Serial.printf("  BT Status      : %s\n", bt_conn ? "CONNECTED" : "WAITING...");
    Serial.printf("  APM RX bytes   : %u\n", g_apm_bytes_rx);
    Serial.printf("  APM TX bytes   : %u\n", g_apm_bytes_tx);
    Serial.printf("  BT  RX bytes   : %u\n", g_bt_bytes_rx);
    Serial.printf("  BT  TX bytes   : %u\n", g_bt_bytes_tx);
    Serial.printf("  MAVLink frames : %u\n", g_mavlink_frames);

    // Hex dump last raw bytes
    Serial.print("  Last raw hex   : ");
    uint8_t count = g_raw_full ? 32 : g_raw_pos;
    // Reconstruct in chronological order
    uint8_t start = g_raw_full ? g_raw_pos : 0;
    for (uint8_t i = 0; i < count; i++) {
        Serial.printf("%02X ", g_raw_buf[(start + i) & 0x1F]);
    }
    Serial.println();
    Serial.println("─────────────────────────────────────────");
}

// ──────────────────────────────────────────────────────────────────────────────
//  Setup
// ──────────────────────────────────────────────────────────────────────────────
void setup() {
    // USB Serial — diagnostic / health monitor
    Serial.begin(115200);
    delay(300);

    Serial.println();
    Serial.println("╔══════════════════════════════════════════╗");
    Serial.println("║   APM 2.8 → ESP32 Bluetooth MAVLink Br. ║");
    Serial.println("╠══════════════════════════════════════════╣");
    Serial.printf( "║  BT Name  : %-28s║\n", BT_DEVICE_NAME);
    Serial.printf( "║  APM RX   : GPIO %-24d║\n", APM_UART_RX);
    Serial.printf( "║  APM TX   : GPIO %-24d║\n", APM_UART_TX);
    Serial.printf( "║  APM Baud : %-28u║\n", APM_BAUD);
    Serial.println("╚══════════════════════════════════════════╝");

    // APM UART — UART2 on GPIO 16/17
    Serial2.begin(APM_BAUD, SERIAL_8N1, APM_UART_RX, APM_UART_TX);

    // Start Bluetooth Serial (Classic SPP)
    if (!SerialBT.begin(BT_DEVICE_NAME)) {
        Serial.println("[ERROR] Bluetooth init FAILED — check board supports BT Classic!");
        // Blink GPIO2 (onboard LED on ESP32 dev boards) to signal error
        pinMode(2, OUTPUT);
        while (true) {
            digitalWrite(2, HIGH); delay(200);
            digitalWrite(2, LOW);  delay(200);
        }
    }
    Serial.println("[BT] Bluetooth started. Pair your phone with \"" BT_DEVICE_NAME "\"");
    Serial.println("[BT] Use baud 57600 in your MAVLink app (QGC / Mission Planner)");

    // Send initial wakeup immediately
    delay(200);
    sendApmWakeup();
    g_last_stream_req  = millis();
    g_last_status_print = millis();
}

// ──────────────────────────────────────────────────────────────────────────────
//  Loop
// ──────────────────────────────────────────────────────────────────────────────
void loop() {
    uint32_t now = millis();

    // ── BT connection events ──────────────────────────────────────────────────
    bool bt_conn = SerialBT.connected();
    if (bt_conn && !g_bt_was_connected) {
        Serial.println("[BT] Client connected — bridge is LIVE");
        // Immediately wake the APM so client gets data right away
        sendApmWakeup();
        g_last_stream_req = now;
    } else if (!bt_conn && g_bt_was_connected) {
        Serial.println("[BT] Client disconnected — restarting SPP server for next client...");
        // BluetoothSerial has a known bug: after disconnect it stops advertising.
        // Restart it so a second device can connect without rebooting the ESP32.
        SerialBT.end();
        delay(200);
        SerialBT.begin(BT_DEVICE_NAME);
        Serial.println("[BT] SPP server restarted — ready for new connection");
    }
    g_bt_was_connected = bt_conn;

    // ── APM UART → Bluetooth ─────────────────────────────────────────────────
    if (Serial2.available()) {
        uint8_t buf[UART_BUF_SIZE];
        int count = 0;
        while (Serial2.available() && count < UART_BUF_SIZE) {
            int b = Serial2.read();
            if (b >= 0) {
                buf[count++] = (uint8_t)b;
                recordRaw((uint8_t)b);
            }
        }
        if (count > 0) {
            g_apm_bytes_rx += count;
            if (bt_conn) {
                SerialBT.write(buf, count);
                g_bt_bytes_tx += count;
            }
        }
    }

    // ── Bluetooth → APM UART ─────────────────────────────────────────────────
    if (bt_conn && SerialBT.available()) {
        uint8_t buf[UART_BUF_SIZE];
        int count = 0;
        while (SerialBT.available() && count < UART_BUF_SIZE) {
            int b = SerialBT.read();
            if (b >= 0) buf[count++] = (uint8_t)b;
        }
        if (count > 0) {
            Serial2.write(buf, count);
            g_bt_bytes_rx  += count;
            g_apm_bytes_tx += count;
        }
    }

    // ── Periodic APM wakeup (GCS heartbeat + stream request) ─────────────────
    if (now - g_last_stream_req >= STREAM_REQUEST_INTERVAL_MS) {
        sendApmWakeup();
        g_last_stream_req = now;
    }

    // ── Periodic health status over USB Serial ────────────────────────────────
    if (now - g_last_status_print >= STATUS_PRINT_INTERVAL_MS) {
        printStatus();
        g_last_status_print = now;
    }
}

/*
 * ═══════════════════════════════════════════════════════════════════════════════
 *  CONNECTION GUIDE — APM 2.8  ↔  ESP32
 * ═══════════════════════════════════════════════════════════════════════════════
 *
 *  APM 2.8 TELEM1 Port (DF13 6-pin)          ESP32 (3.3V logic)
 *  ───────────────────────────────────        ─────────────────────────────
 *  Pin 1  — +5V  ──────────────────────────►  VIN  (powers ESP32 via USB reg)
 *  Pin 2  — TX   ──────────────────────────►  GPIO 16  (UART2 RX — 3.3V safe)
 *  Pin 3  — RX   ◄── Voltage Divider ──────   GPIO 17  (UART2 TX)
 *  Pin 4  — CTS  (leave unconnected)
 *  Pin 5  — GND  ──────────────────────────►  GND
 *  Pin 6  — (none)
 *
 *  ⚠  VOLTAGE DIVIDER for APM RX line (5V tolerant, but APM RX expects 3.3V drive):
 *      ESP32 GPIO17 ──[1kΩ]──┬──[2kΩ]── GND
 *                             └──────────────► APM TELEM1 RX (Pin 3)
 *     (This divides 3.3V → 2.2V which the APM reads as logic HIGH reliably)
 *     OR simply use a bi-directional logic level shifter (recommended).
 *
 *  ALTERNATIVE — power ESP32 from its own USB:
 *      Just connect GND, TX→GPIO16, RX→GPIO17 (with divider). Skip VIN.
 *
 *  PHONE / GCS PAIRING:
 *    1. Enable Bluetooth on phone
 *    2. Pair with "APM-Bridge" (no PIN required)
 *    3. In QGroundControl → Comm Links → Add → Bluetooth → select "APM-Bridge"
 *       OR Mission Planner → COM port dropdown → select "APM-Bridge"
 *    4. Set baud 57600 — connect!
 *
 * ═══════════════════════════════════════════════════════════════════════════════
 */

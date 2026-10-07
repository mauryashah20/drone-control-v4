package com.example.udpandroidapptransmitter

import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothSocket
import android.util.Log
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.util.UUID
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

/**
 * Bluetooth SPP MAVLink bridge for ESP32 "APM-Bridge".
 *
 * Uplink:  ESP32 BT → phone → UDP → Laptop (port 14551)
 * Downlink: Laptop UDP → phone → ESP32 BT → APM
 */
class BluetoothTelemetryBridge(
    var btDeviceName: String = "APM-Bridge",
    var laptopIp: String = "192.168.191.187",
    var laptopPort: Int = 14551,
    var laptopListenPort: Int = 14551
) {
    companion object {
        private const val TAG = "BtTelemetryBridge"
        private val SPP_UUID: UUID = UUID.fromString("00001101-0000-1000-8000-00805F9B34FB")
        private const val BUFFER_SIZE = 4096
        private const val RECONNECT_DELAY_MS = 3000L
    }

    private val isRunning = AtomicBoolean(false)

    private var btSocket: BluetoothSocket? = null
    private var connectingSocket: BluetoothSocket? = null
    private var udpSocket: DatagramSocket? = null

    private var btToUdpThread: Thread? = null
    private var udpToBtThread: Thread? = null

    val uplinkPackets  = AtomicLong(0)
    val downlinkPackets = AtomicLong(0)
    val uplinkBytes    = AtomicLong(0)
    val downlinkBytes  = AtomicLong(0)

    @Volatile var isConnected = false

    @Volatile private var targetAddr: InetAddress? = try { InetAddress.getByName(laptopIp) } catch (e: Exception) { null }

    fun updateTargetIp(newIp: String) {
        laptopIp = newIp
        try {
            targetAddr = InetAddress.getByName(newIp)
            Log.i(TAG, "BluetoothTelemetryBridge updated target IP to $newIp")
        } catch (e: Exception) {
            Log.e(TAG, "Bad new laptop IP $newIp: ${e.message}")
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    fun start() {
        if (isRunning.getAndSet(true)) return

        uplinkPackets.set(0); downlinkPackets.set(0)
        uplinkBytes.set(0);   downlinkBytes.set(0)

        // UDP socket to talk to the laptop (same as before)
        try {
            udpSocket = DatagramSocket(null).apply {
                reuseAddress = true
                bind(InetSocketAddress(laptopListenPort))
            }
        } catch (e: Exception) {
            Log.e(TAG, "UDP socket bind failed: ${e.message}")
            isRunning.set(false)
            return
        }

        // BT → UDP uplink thread (handles connect + reconnect internally)
        btToUdpThread = Thread({
            try {
                while (isRunning.get() && !Thread.currentThread().isInterrupted) {
                val socket = connectBluetooth()
                if (socket == null) {
                    try {
                        Thread.sleep(RECONNECT_DELAY_MS)
                    } catch (_: InterruptedException) {
                        break
                    }
                    continue
                }
                btSocket = socket
                isConnected = true
                Log.i(TAG, "BT connected to $btDeviceName — bridge live")

                try {
                    val input = socket.inputStream
                    val buf = ByteArray(BUFFER_SIZE)
                    while (isRunning.get() && !Thread.currentThread().isInterrupted) {
                        val n = input.read(buf)
                        if (n <= 0) break
                        val addr = targetAddr ?: continue
                        // Forward raw MAVLink bytes to laptop via UDP
                        val udp = udpSocket ?: break
                        val pkt = DatagramPacket(buf, 0, n, addr, laptopPort)
                        udp.send(pkt)
                        uplinkPackets.incrementAndGet()
                        uplinkBytes.addAndGet(n.toLong())
                    }
                } catch (e: Exception) {
                    if (isRunning.get()) Log.w(TAG, "BT read error: ${e.message}")
                } finally {
                    isConnected = false
                    try { socket.close() } catch (_: Exception) {}
                    btSocket = null
                    if (isRunning.get() && !Thread.currentThread().isInterrupted) {
                        Log.i(TAG, "BT disconnected — reconnecting in ${RECONNECT_DELAY_MS}ms")
                        try {
                            Thread.sleep(RECONNECT_DELAY_MS)
                        } catch (_: InterruptedException) {
                            break
                        }
                    }
                }
            }
        } catch (_: InterruptedException) {
                // Thread interrupted cleanly on stop
            } catch (t: Throwable) {
                Log.e(TAG, "BT-Uplink thread error: ${t.message}", t)
            } finally {
                isConnected = false
            }
        }, "BT-Uplink").apply { priority = Thread.NORM_PRIORITY + 1; start() }

        // UDP → BT downlink thread (GCS commands from laptop back to APM via ESP32)
        udpToBtThread = Thread({
            try {
                val buf = ByteArray(BUFFER_SIZE)
                val pkt = DatagramPacket(buf, buf.size)
                while (isRunning.get() && !Thread.currentThread().isInterrupted) {
                    try {
                        val udp = udpSocket ?: break
                        udp.receive(pkt)
                        val n = pkt.length
                        if (n <= 0) continue
                        val socket = btSocket
                        if (socket != null && socket.isConnected) {
                            socket.outputStream.write(buf, 0, n)
                            socket.outputStream.flush()
                            downlinkPackets.incrementAndGet()
                            downlinkBytes.addAndGet(n.toLong())
                        }
                    } catch (e: Exception) {
                        if (isRunning.get()) Log.w(TAG, "UDP recv error: ${e.message}")
                    }
                }
            } catch (_: InterruptedException) {
                // Thread interrupted cleanly on stop
            } catch (t: Throwable) {
                Log.e(TAG, "BT-Downlink thread error: ${t.message}", t)
            }
        }, "BT-Downlink").apply { priority = Thread.NORM_PRIORITY + 1; start() }

        Log.i(TAG, "BluetoothTelemetryBridge started → searching for $btDeviceName")
    }

    // ─────────────────────────────────────────────────────────────────────────
    private fun connectBluetooth(): BluetoothSocket? {
        return try {
            val adapter = BluetoothAdapter.getDefaultAdapter()
                ?: run { Log.e(TAG, "No BT adapter"); return null }

            // Find already-paired device by name OR MAC address
            @Suppress("MissingPermission")
            val device: BluetoothDevice = adapter.bondedDevices
                .firstOrNull { it.name == btDeviceName || it.address == btDeviceName }
                ?: run {
                    Log.w(TAG, "\"$btDeviceName\" not paired yet — pair it in Android Settings → Bluetooth")
                    return null
                }

            Log.i(TAG, "Found paired device: ${device.name} [${device.address}] — connecting via RFCOMM ch1…")

            // PRIMARY: reflection-based direct channel-1 socket.
            // ESP32 BluetoothSerial always uses RFCOMM channel 1.
            // This bypasses the SDP lookup that times out on ESP32.
            val socket: BluetoothSocket = try {
                @Suppress("MissingPermission")
                val method = device.javaClass.getMethod("createRfcommSocket", Int::class.java)
                method.invoke(device, 1) as BluetoothSocket
            } catch (e: Exception) {
                Log.w(TAG, "Reflection socket failed (${e.message}), falling back to UUID…")
                // FALLBACK: standard insecure UUID-based socket
                @Suppress("MissingPermission")
                device.createInsecureRfcommSocketToServiceRecord(SPP_UUID)
            }

            connectingSocket = socket
            @Suppress("MissingPermission")
            adapter.cancelDiscovery()
            socket.connect()   // blocks until connected or throws
            connectingSocket = null
            socket
        } catch (e: Exception) {
            connectingSocket = null
            Log.w(TAG, "BT connect failed: ${e.message}")
            null
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    fun stop() {
        if (!isRunning.getAndSet(false)) return
        isConnected = false
        try { connectingSocket?.close() } catch (_: Exception) {}
        connectingSocket = null
        try { btSocket?.close() }   catch (_: Exception) {}
        btSocket = null
        try { udpSocket?.close() }  catch (_: Exception) {}
        udpSocket = null
        btToUdpThread?.interrupt()
        udpToBtThread?.interrupt()
        btToUdpThread = null
        udpToBtThread = null
        Log.i(TAG, "BluetoothTelemetryBridge stopped")
    }

    fun isAlive(): Boolean = isRunning.get()
}

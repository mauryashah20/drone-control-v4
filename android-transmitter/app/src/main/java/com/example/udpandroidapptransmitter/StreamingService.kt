package com.example.udpandroidapptransmitter

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.*
import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaFormat
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.IBinder
import android.os.Process
import android.util.Log
import android.util.Range
import android.util.Size
import android.view.Surface
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class StreamingService : Service() {

    companion object {
        const val TAG = "StreamingService"
        const val CHANNEL_ID = "drone_stream_channel"

        // Actions
        const val ACTION_START = "com.example.udpandroidapptransmitter.START"
        const val ACTION_STOP = "com.example.udpandroidapptransmitter.STOP"
        const val ACTION_STATS_BROADCAST = "com.example.udpandroidapptransmitter.STATS"

        // Extras
        const val EXTRA_TARGET_IP = "target_ip"
        const val EXTRA_TARGET_PORT = "target_port"
        const val EXTRA_WIDTH = "width"
        const val EXTRA_HEIGHT = "height"
        const val EXTRA_FPS = "fps"
        const val EXTRA_BITRATE = "bitrate"
        const val EXTRA_AUTO_QUALITY = "auto_quality"

        const val EXTRA_LIVE_FPS = "live_fps"
        const val EXTRA_LIVE_KBPS = "live_kbps"
        const val EXTRA_LIVE_PACKETS = "live_packets"

        // Telemetry Extras
        const val EXTRA_ENABLE_TELEMETRY = "enable_telemetry"
        const val EXTRA_ESP_IP = "esp_ip"
        const val EXTRA_TELEMETRY_PORT = "telemetry_port"
        const val EXTRA_TELEM_UPLINK = "telem_uplink"
        const val EXTRA_TELEM_DOWNLINK = "telem_downlink"
        const val EXTRA_TELEM_ACTIVE = "telem_active"

        // Control Packet Signatures (Header 0xFF)
        val PACKET_KEEP_ALIVE = byteArrayOf(0xFF.toByte(), 0xAA.toByte(), 0x55.toByte(), 0x00.toByte())
        val PACKET_HELLO = byteArrayOf(0xFF.toByte(), 0x01.toByte(), 0xCA.toByte(), 0xFE.toByte())
        val PACKET_GOODBYE = byteArrayOf(0xFF.toByte(), 0xDE.toByte(), 0xAD.toByte(), 0x01.toByte())
        val PACKET_APP_CLOSE = byteArrayOf(0xFF.toByte(), 0xDE.toByte(), 0xAD.toByte(), 0x02.toByte())
    }

    private val isStreaming = AtomicBoolean(false)
    private val encoderExecutor = Executors.newSingleThreadExecutor()
    private val feedbackExecutor = Executors.newSingleThreadExecutor()

    private val cameraThread: HandlerThread by lazy {
        HandlerThread("camera-zero-latency").apply { start() }
    }
    private val cameraHandler: Handler by lazy { Handler(cameraThread.looper) }

    private var cameraDevice: CameraDevice? = null
    private var captureSession: CameraCaptureSession? = null
    private var inputSurface: Surface? = null
    private var encoder: MediaCodec? = null

    // Network Sockets
    private var udpSocket: DatagramSocket? = null
    private var udpTarget: InetSocketAddress? = null

    // Stream Configuration
    private var targetWidth = 640
    private var targetHeight = 480
    private var targetFps = 30
    private var targetBitrate = 700_000
    private var isAutoQuality = true
    private var currentBitrate = 700_000
    private var lastBitrateAdjustTime = 0L
    private var targetIp = "2401:4900:8f73:7949:8fa7:f1ad:81c1:b5b2"
    private var targetPort = 5005

    // Telemetry Bridge — Bluetooth SPP
    private var telemetryBridge: BluetoothTelemetryBridge? = null
    private var isTelemetryEnabled = true
    private var btDeviceName = "APM-Bridge"
    private var telemetryPort = 14551

    // Packet buffers - 1150 bytes safe MTU avoids cellular carrier fragmentation
    private val mtuBytes = 1150
    private val headerSize = 16 // 4 frame_seq + 2 chunk_idx + 2 total_chunks + 8 ts_ms
    private val payloadMtu = mtuBytes - headerSize
    private val videoPacketBuffer = ByteArray(2048)

    private var wakeLock: android.os.PowerManager.WakeLock? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action ?: ACTION_START
        if (action == ACTION_STOP) {
            stopStreaming()
            return START_NOT_STICKY
        }

        if (isStreaming.get()) {
            Log.w(TAG, "Streaming was active - resetting session for new start command")
            stopStreaming()
        }

        // Extract parameters
        targetIp = intent?.getStringExtra(EXTRA_TARGET_IP) ?: "2401:4900:8f73:7949:8fa7:f1ad:81c1:b5b2"
        targetPort = intent?.getIntExtra(EXTRA_TARGET_PORT, 5005) ?: 5005
        targetWidth = intent?.getIntExtra(EXTRA_WIDTH, 640) ?: 640
        targetHeight = intent?.getIntExtra(EXTRA_HEIGHT, 480) ?: 480
        targetFps = intent?.getIntExtra(EXTRA_FPS, 30) ?: 30
        targetBitrate = intent?.getIntExtra(EXTRA_BITRATE, 700_000) ?: 700_000
        isAutoQuality = intent?.getBooleanExtra(EXTRA_AUTO_QUALITY, true) ?: true
        currentBitrate = targetBitrate

        isTelemetryEnabled = intent?.getBooleanExtra(EXTRA_ENABLE_TELEMETRY, true) ?: true
        btDeviceName = intent?.getStringExtra(EXTRA_ESP_IP) ?: "APM-Bridge"  // field reused for BT name
        telemetryPort = intent?.getIntExtra(EXTRA_TELEMETRY_PORT, 14551) ?: 14551

        if (isTelemetryEnabled) {
            try {
                telemetryBridge?.stop()
                telemetryBridge = BluetoothTelemetryBridge(
                    btDeviceName = btDeviceName,
                    laptopIp = targetIp,
                    laptopPort = telemetryPort,
                    laptopListenPort = telemetryPort
                ).apply { start() }
                Log.i(TAG, "BluetoothTelemetryBridge started: BT=$btDeviceName → $targetIp:$telemetryPort")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to initialize BT telemetry bridge", e)
            }
        }

        startForeground(101, buildNotification("Streaming to $targetIp:$targetPort (${targetWidth}x${targetHeight} @ ${targetFps}fps)"))

        isStreaming.set(true)

        try {
            val pm = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
            wakeLock = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "DroneStreamer::WakeLock").apply {
                acquire(12 * 60 * 60 * 1000L)
            }
        } catch (_: Exception) {}

        // Setup socket and pipeline
        try {
            udpSocket = DatagramSocket()
            udpTarget = InetSocketAddress(targetIp, targetPort)

            // Send Hello packet to inform receiver immediately of new session
            sendControlPacket(PACKET_HELLO, 3)

            startFeedbackListener()

            cameraHandler.post {
                try {
                    setupHardwareEncoder()
                    openCameraAndStartCapture()
                } catch (e: Exception) {
                    Log.e(TAG, "Pipeline startup failed", e)
                    stopStreaming()
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Socket initialization failed", e)
            stopStreaming()
        }

        return START_STICKY
    }

    private fun startFeedbackListener() {
        feedbackExecutor.execute {
            val buf = ByteArray(32)
            val packet = DatagramPacket(buf, buf.size)
            while (isStreaming.get()) {
                try {
                    val socket = udpSocket ?: break
                    socket.receive(packet)
                    if (packet.length >= 2 && isAutoQuality) {
                        val latencyMs = ((buf[0].toInt() and 0xFF) shl 8) or (buf[1].toInt() and 0xFF)
                        adjustBitrateForLatency(latencyMs)
                    }
                } catch (_: Throwable) {
                    if (!isStreaming.get()) break
                }
            }
        }
    }

    private fun adjustBitrateForLatency(latencyMs: Int) {
        if (!isAutoQuality) return
        val now = System.currentTimeMillis()
        if (now - lastBitrateAdjustTime < 200) return

        // User requirement: latency must NEVER exceed 150ms due to image quality
        // If latency climbs above 150ms, immediately drop bitrate to drain queue!
        val newBitrate = when {
            latencyMs > 150 -> maxOf(250_000, currentBitrate - 250_000)
            latencyMs > 135 -> maxOf(350_000, currentBitrate - 120_000)
            latencyMs <= 115 && currentBitrate < 1_600_000 -> currentBitrate + 75_000
            else -> currentBitrate
        }

        if (newBitrate != currentBitrate) {
            currentBitrate = newBitrate
            val params = android.os.Bundle().apply {
                putInt(MediaCodec.PARAMETER_KEY_VIDEO_BITRATE, newBitrate)
            }
            try {
                encoder?.setParameters(params)
                Log.d(TAG, "Auto adaptive bitrate set to ${newBitrate / 1000} kbps (latency: ${latencyMs}ms)")
            } catch (_: Exception) {}
            lastBitrateAdjustTime = now
        }
    }

    private fun teardownSync() {
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (_: Exception) {}
        wakeLock = null

        try {
            captureSession?.stopRepeating()
            captureSession?.abortCaptures()
            captureSession?.close()
        } catch (_: Throwable) {}
        captureSession = null

        try { cameraDevice?.close() } catch (_: Throwable) {}
        cameraDevice = null

        try {
            encoder?.stop()
            encoder?.release()
        } catch (_: Throwable) {}
        encoder = null
        inputSurface = null

        try { udpSocket?.close() } catch (_: Throwable) {}
        udpSocket = null
        isStreaming.set(false)
    }

    private fun setupHardwareEncoder() {
        Log.i(TAG, "Configuring Qualcomm Hardware AVC Encoder: ${targetWidth}x${targetHeight} @ ${targetFps}fps, ${targetBitrate / 1000}kbps")

        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, targetWidth, targetHeight).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, targetBitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, targetFps)
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1)

            // Constant Bitrate mode
            setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_CBR)

            // Baseline profile - strictly prohibits B-frames (0 frame lookahead)
            setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
            setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)

            // Real-time priority
            setInteger(MediaFormat.KEY_PRIORITY, 0)

            // Ultra-Low Latency Mode (Android 11+)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                try { setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0) } catch (_: Exception) {}
            }
            try { setInteger(MediaFormat.KEY_LATENCY, 0) } catch (_: Exception) {}

            // Prepend SPS/PPS with sync frames for instant decoder sync
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1)
            }
        }

        val codec = MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        inputSurface = codec.createInputSurface()
        codec.start()
        encoder = codec

        // Start encoder draining thread
        encoderExecutor.execute { drainEncoderLoop() }
    }

    private fun openCameraAndStartCapture() {
        val cameraManager = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = getBackCameraId(cameraManager)

        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            throw SecurityException("Camera permission not granted")
        }

        cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
            override fun onOpened(device: CameraDevice) {
                cameraDevice = device
                createCaptureSession(device)
            }

            override fun onDisconnected(device: CameraDevice) {
                device.close()
                cameraDevice = null
            }

            override fun onError(device: CameraDevice, error: Int) {
                Log.e(TAG, "Camera device error: $error")
                device.close()
                cameraDevice = null
            }
        }, cameraHandler)
    }

    private fun getBestFpsRange(manager: CameraManager, cameraId: String, desiredFps: Int): Range<Int> {
        val chars = manager.getCameraCharacteristics(cameraId)
        val ranges = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES) ?: return Range(30, 30)

        // 1. Look for fixed range [desiredFps, desiredFps]
        for (r in ranges) {
            if (r.lower == desiredFps && r.upper == desiredFps) return r
        }
        // 2. Look for range where upper is desiredFps
        var bestUpper: Range<Int>? = null
        for (r in ranges) {
            if (r.upper == desiredFps) {
                if (bestUpper == null || r.lower > bestUpper.lower) {
                    bestUpper = r
                }
            }
        }
        if (bestUpper != null) return bestUpper

        // 3. Otherwise find range closest to desiredFps
        var bestRange = ranges[0]
        var minDiff = Int.MAX_VALUE
        for (r in ranges) {
            val diff = Math.abs(r.upper - desiredFps) * 100 + (r.upper - r.lower)
            if (diff < minDiff) {
                minDiff = diff
                bestRange = r
            }
        }
        return bestRange
    }

    private fun createCaptureSession(device: CameraDevice) {
        val encoderSurface = inputSurface ?: throw IllegalStateException("Encoder surface is null")
        val cameraManager = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val fpsRange = getBestFpsRange(cameraManager, device.id, targetFps)
        Log.i(TAG, "Creating dedicated headless encoder capture session, AE FPS range: $fpsRange")

        // Single surface direct pipeline: Camera -> Hardware AVC Encoder (0 GPU preview rendering overhead)
        device.createCaptureSession(listOf(encoderSurface), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(session: CameraCaptureSession) {
                if (cameraDevice == null) return
                captureSession = session

                try {
                    val requestBuilder = device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
                        addTarget(encoderSurface)

                        set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)
                        set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fpsRange)

                        // Disable all stabilization (OIS and EIS off as requested)
                        set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
                        set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)

                        // Enable CONTINUOUS_PICTURE autofocus for rapid, razor-sharp focus
                        set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE)

                        // Enable FAST edge enhancement and noise filtering to eliminate blurriness
                        set(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_FAST)
                        set(CaptureRequest.NOISE_REDUCTION_MODE, CameraMetadata.NOISE_REDUCTION_MODE_FAST)

                        // Zero-delay direct pass
                        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                            try { set(CaptureRequest.DISTORTION_CORRECTION_MODE, CameraMetadata.DISTORTION_CORRECTION_MODE_OFF) } catch (_: Exception) {}
                        }
                        set(CaptureRequest.HOT_PIXEL_MODE, CameraMetadata.HOT_PIXEL_MODE_OFF)
                        set(CaptureRequest.SHADING_MODE, CameraMetadata.SHADING_MODE_OFF)
                        set(CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE, CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF)
                        set(CaptureRequest.TONEMAP_MODE, CameraMetadata.TONEMAP_MODE_FAST)
                    }

                    session.setRepeatingRequest(requestBuilder.build(), object : CameraCaptureSession.CaptureCallback() {
                        override fun onCaptureFailed(session: CameraCaptureSession, request: CaptureRequest, failure: CaptureFailure) {
                            Log.w(TAG, "Frame capture failed: reason ${failure.reason}")
                        }
                    }, cameraHandler)
                    Log.i(TAG, "Headless camera capture session active (Single Surface Direct Pipe, Continuous Autofocus)")
                } catch (e: Exception) {
                    Log.e(TAG, "Failed to start repeating request on encoder surface", e)
                }
            }

            override fun onConfigureFailed(session: CameraCaptureSession) {
                Log.e(TAG, "Encoder-only capture session failed! Check surface size $targetWidth x $targetHeight")
            }
        }, cameraHandler)
    }

    private fun drainEncoderLoop() {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_DISPLAY)
        val codec = encoder ?: return
        val bufferInfo = MediaCodec.BufferInfo()
        var sequence: Long = 0
        var bytesSentSinceReport: Long = 0
        var packetsSentSinceReport: Long = 0
        var framesSentSinceReport: Long = 0
        var lastStatsTime = System.currentTimeMillis()
        var lastPacketSentTime = System.currentTimeMillis()

        while (isStreaming.get()) {
            try {
                // Non-blocking poll (0us) for strictly zero buffering
                val outIndex = codec.dequeueOutputBuffer(bufferInfo, 0)
                val now = System.currentTimeMillis()

                if (outIndex >= 0) {
                    val encodedBuffer = codec.getOutputBuffer(outIndex)
                    if (encodedBuffer != null && bufferInfo.size > 0) {
                        encodedBuffer.position(bufferInfo.offset)
                        encodedBuffer.limit(bufferInfo.offset + bufferInfo.size)

                        val pkts = sendDirectChunkedUdp(encodedBuffer, sequence)
                        packetsSentSinceReport += pkts
                        bytesSentSinceReport += bufferInfo.size
                        framesSentSinceReport += 1
                        sequence++
                        lastPacketSentTime = now
                    }
                    codec.releaseOutputBuffer(outIndex, false)
                } else {
                    // Option 3: If no frame was sent for 15ms, send a tiny dummy packet
                    // to keep the phone's 5G/4G modem in permanent active RRC state
                    if (now - lastPacketSentTime >= 15) {
                        sendCellularKeepAlive()
                        lastPacketSentTime = now
                    }
                    // Yield micro-timeslice to prevent 100% CPU thread starvation
                    Thread.yield()
                }

                // Periodic telemetry reporting to UI
                val elapsed = now - lastStatsTime
                if (elapsed >= 1000) {
                    val currentFps = (framesSentSinceReport * 1000.0f) / elapsed
                    val currentKbps = (bytesSentSinceReport * 8.0f) / elapsed
                    broadcastStats(currentFps, currentKbps, sequence)

                    bytesSentSinceReport = 0
                    packetsSentSinceReport = 0
                    framesSentSinceReport = 0
                    lastStatsTime = now
                }
            } catch (t: Throwable) {
                if (isStreaming.get()) Log.w(TAG, "Encoder poll exception", t)
            }
        }
    }

    private fun sendControlPacket(bytes: ByteArray, times: Int = 3) {
        val socket = udpSocket ?: return
        val target = udpTarget ?: return
        try {
            val packet = DatagramPacket(bytes, bytes.size, target.address, target.port)
            for (i in 0 until times) {
                socket.send(packet)
            }
        } catch (_: Exception) {}
    }

    private fun sendCellularKeepAlive() {
        sendControlPacket(PACKET_KEEP_ALIVE, 1)
    }

    /**
     * Chunk and transmit NAL units over UDP with 16-byte robust header
     */
    private fun sendDirectChunkedUdp(buffer: ByteBuffer, sequence: Long): Int {
        val socket = udpSocket ?: return 0
        val target = udpTarget ?: return 0

        val totalSize = buffer.remaining()
        val totalChunks = (totalSize + payloadMtu - 1) / payloadMtu
        val timestampMs = System.currentTimeMillis()
        val frameSeqInt = (sequence and 0xFFFFFFFFL).toInt()

        var packetsCount = 0

        for (chunkIdx in 0 until totalChunks) {
            val chunkSize = minOf(payloadMtu, buffer.remaining())

            // 16-Byte Header format:
            // 0..3: Frame Sequence (Int, 4B)
            // 4..5: Chunk Index (Short, 2B)
            // 6..7: Total Chunks (Short, 2B)
            // 8..15: Timestamp in ms (Long, 8B)
            writeInt(videoPacketBuffer, 0, frameSeqInt)
            writeShort(videoPacketBuffer, 4, chunkIdx.toShort())
            writeShort(videoPacketBuffer, 6, totalChunks.toShort())
            writeLong(videoPacketBuffer, 8, timestampMs)

            // Copy chunk directly from native ByteBuffer to packet buffer
            buffer.get(videoPacketBuffer, headerSize, chunkSize)

            val packet = DatagramPacket(videoPacketBuffer, headerSize + chunkSize, target.address, target.port)
            try {
                socket.send(packet)
                packetsCount++
            } catch (_: Exception) {}
        }

        return packetsCount
    }

    private fun writeInt(dest: ByteArray, offset: Int, value: Int) {
        dest[offset + 0] = (value ushr 24).toByte()
        dest[offset + 1] = (value ushr 16).toByte()
        dest[offset + 2] = (value ushr 8).toByte()
        dest[offset + 3] = value.toByte()
    }

    private fun writeShort(dest: ByteArray, offset: Int, value: Short) {
        dest[offset + 0] = (value.toInt() ushr 8).toByte()
        dest[offset + 1] = value.toByte()
    }

    private fun writeLong(dest: ByteArray, offset: Int, value: Long) {
        dest[offset + 0] = (value ushr 56).toByte()
        dest[offset + 1] = (value ushr 48).toByte()
        dest[offset + 2] = (value ushr 40).toByte()
        dest[offset + 3] = (value ushr 32).toByte()
        dest[offset + 4] = (value ushr 24).toByte()
        dest[offset + 5] = (value ushr 16).toByte()
        dest[offset + 6] = (value ushr 8).toByte()
        dest[offset + 7] = value.toByte()
    }

    private fun broadcastStats(fps: Float, kbps: Float, totalPackets: Long) {
        val telemUp = telemetryBridge?.uplinkPackets?.get() ?: 0L
        val telemDown = telemetryBridge?.downlinkPackets?.get() ?: 0L
        val telemActive = telemetryBridge?.isAlive() ?: false

        val intent = Intent(ACTION_STATS_BROADCAST).apply {
            putExtra(EXTRA_LIVE_FPS, fps)
            putExtra(EXTRA_LIVE_KBPS, kbps)
            putExtra(EXTRA_LIVE_PACKETS, totalPackets)
            putExtra(EXTRA_TELEM_UPLINK, telemUp)
            putExtra(EXTRA_TELEM_DOWNLINK, telemDown)
            putExtra(EXTRA_TELEM_ACTIVE, telemActive)
            setPackage(packageName)
        }
        sendBroadcast(intent)
    }

    private fun getBackCameraId(manager: CameraManager): String {
        for (id in manager.cameraIdList) {
            val chars = manager.getCameraCharacteristics(id)
            val facing = chars.get(CameraCharacteristics.LENS_FACING)
            if (facing == CameraCharacteristics.LENS_FACING_BACK) {
                return id
            }
        }
        return manager.cameraIdList.firstOrNull() ?: "0"
    }

    private fun stopStreaming() {
        if (!isStreaming.getAndSet(false)) return

        // Send instant Goodbye packets to receiver before closing socket
        sendControlPacket(PACKET_GOODBYE, 3)

        cameraHandler.post {
            try {
                captureSession?.stopRepeating()
                captureSession?.abortCaptures()
                captureSession?.close()
            } catch (_: Throwable) {}
            captureSession = null

            try {
                cameraDevice?.close()
            } catch (_: Throwable) {}
            cameraDevice = null

            try {
                encoder?.stop()
                encoder?.release()
            } catch (_: Throwable) {}
            encoder = null
            inputSurface = null

            try {
                udpSocket?.close()
            } catch (_: Throwable) {}
            udpSocket = null

            try {
                telemetryBridge?.stop()
            } catch (_: Throwable) {}
            telemetryBridge = null

            stopForeground(true)
            stopSelf()
            Log.i(TAG, "Streaming service stopped and cleaned up")
        }
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        sendControlPacket(PACKET_APP_CLOSE, 3)
        stopStreaming()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        sendControlPacket(PACKET_APP_CLOSE, 3)
        stopStreaming()
        cameraThread.quitSafely()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val mgr = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            val channel = NotificationChannel(CHANNEL_ID, "Drone Streamer", NotificationManager.IMPORTANCE_LOW)
            mgr.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(text: String): Notification {
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Drone Stream Active")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.presence_video_online)
            .setOngoing(true)
            .build()
    }
}

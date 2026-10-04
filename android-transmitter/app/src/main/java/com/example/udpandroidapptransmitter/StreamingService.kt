package com.example.udpandroidapptransmitter

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
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
import android.view.Surface
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.util.concurrent.ExecutorService
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
        const val ACTION_STREAMING_STOPPED = "com.example.udpandroidapptransmitter.STOPPED"

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
    private val stateLock = Any()

    @Volatile
    private var encoderExecutor: ExecutorService? = null

    private var cameraThread: HandlerThread? = null
    private var cameraHandler: Handler? = null

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

    @Synchronized
    private fun getCameraHandler(): Handler {
        val thread = cameraThread
        if (thread == null || !thread.isAlive) {
            val newThread = HandlerThread("camera-zero-latency").apply { start() }
            cameraThread = newThread
            cameraHandler = Handler(newThread.looper)
        }
        return cameraHandler!!
    }

    @Synchronized
    private fun getEncoderExecutor(): ExecutorService {
        val exec = encoderExecutor
        if (exec == null || exec.isShutdown) {
            val newExec = Executors.newSingleThreadExecutor()
            encoderExecutor = newExec
            return newExec
        }
        return exec
    }

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

        synchronized(stateLock) {
            if (isStreaming.get()) {
                Log.w(TAG, "Streaming was active - resetting session for new start command")
                stopStreamingInternal()
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
            btDeviceName = intent?.getStringExtra(EXTRA_ESP_IP) ?: "APM-Bridge"
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

            val notification = buildNotification("Streaming to $targetIp:$targetPort (${targetWidth}x${targetHeight} @ ${targetFps}fps)")
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ServiceCompat.startForeground(
                    this,
                    101,
                    notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA
                )
            } else {
                startForeground(101, notification)
            }

            isStreaming.set(true)

            try {
                val pm = getSystemService(Context.POWER_SERVICE) as android.os.PowerManager
                wakeLock = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "DroneStreamer::WakeLock").apply {
                    acquire(12 * 60 * 60 * 1000L)
                }
            } catch (_: Exception) {}

            try {
                udpSocket = DatagramSocket().apply {
                    try {
                        // Pure FPV: Small socket send buffer (32KB) prevents Android OS kernel
                        // from queueing stale video packets during cellular jitter bursts
                        sendBufferSize = 32 * 1024
                    } catch (_: Exception) {}
                }
                udpTarget = InetSocketAddress(targetIp, targetPort)

                startUdpCommandListener(udpSocket!!)
                sendControlPacket(PACKET_HELLO, 3)

                getCameraHandler().post {
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
        }

        return START_NOT_STICKY
    }

    private fun stopHardwareEncoder() {
        val oldEncoder = encoder
        encoder = null
        val oldSurface = inputSurface
        inputSurface = null

        if (oldEncoder != null) {
            try { oldEncoder.stop() } catch (_: Throwable) {}
            try { oldEncoder.release() } catch (_: Throwable) {}
        }
        if (oldSurface != null) {
            try { oldSurface.release() } catch (_: Throwable) {}
        }
    }

    private fun createBestQualcommEncoder(): MediaCodec {
        val candidateNames = listOf(
            "c2.qti.avc.encoder.low_latency",
            "c2.qti.avc.encoder",
            "OMX.qcom.video.encoder.avc"
        )
        for (name in candidateNames) {
            try {
                val codec = MediaCodec.createByCodecName(name)
                Log.i(TAG, "Selected optimized Qualcomm hardware encoder: $name")
                return codec
            } catch (_: Exception) {}
        }
        Log.i(TAG, "Falling back to default AVC encoder")
        return MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_VIDEO_AVC)
    }

    private fun setupHardwareEncoder() {
        Log.i(TAG, "Configuring Qualcomm Hardware AVC Encoder: ${targetWidth}x${targetHeight} @ ${targetFps}fps, ${targetBitrate / 1000}kbps")

        // Release any existing encoder instance before creating a new one
        stopHardwareEncoder()

        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, targetWidth, targetHeight).apply {
            setInteger(MediaFormat.KEY_COLOR_FORMAT, MediaCodecInfo.CodecCapabilities.COLOR_FormatSurface)
            setInteger(MediaFormat.KEY_BIT_RATE, targetBitrate)
            setInteger(MediaFormat.KEY_FRAME_RATE, targetFps)
            // 60-second I-frame interval: eliminates periodic 1-2s IDR keyframe burst spikes.
            // Intra-refresh smoothly updates macroblocks every frame instead.
            setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 60)

            // Pure FPV: VBR (Variable Bitrate) mode - ZERO buffer smoothing, zero lookahead delay!
            // CBR mode forces the encoder to buffer frames to smooth the bit stream.
            // VBR encodes each frame immediately without HRD/VBV buffer latency.
            setInteger(MediaFormat.KEY_BITRATE_MODE, MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)
            setInteger(MediaFormat.KEY_PROFILE, MediaCodecInfo.CodecProfileLevel.AVCProfileBaseline)
            setInteger(MediaFormat.KEY_LEVEL, MediaCodecInfo.CodecProfileLevel.AVCLevel31)
            setInteger(MediaFormat.KEY_PRIORITY, 0) // Real-time priority

            // 1. VPU clock governor boost: force Qualcomm VPU to run at maximum operating frequency
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                try { setFloat(MediaFormat.KEY_OPERATING_RATE, 240.0f) } catch (_: Exception) {}
                try { setInteger(MediaFormat.KEY_OPERATING_RATE, 240) } catch (_: Exception) {}
            }

            // 2. Periodic Intra Refresh (PIR): smooth out cellular spikes by refreshing macroblocks across frames
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                try { setInteger(MediaFormat.KEY_INTRA_REFRESH_PERIOD, targetFps) } catch (_: Exception) {}
            }
            try {
                setInteger("vendor.qti-ext-enc-intra-refresh.mode", 1) // Cyclic column/row refresh
                setInteger("vendor.qti-ext-enc-intra-refresh.period", targetFps)
            } catch (_: Exception) {}

            // 3. Android Low-latency & zero-lookahead flags
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                try { setInteger(MediaFormat.KEY_MAX_B_FRAMES, 0) } catch (_: Exception) {}
            }
            try { setInteger(MediaFormat.KEY_LATENCY, 0) } catch (_: Exception) {}

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                setInteger(MediaFormat.KEY_PREPEND_HEADER_TO_SYNC_FRAMES, 1)
            }

            // 4. Qualcomm hardware low-latency & slice delivery extensions
            try { setInteger("vendor.qti-ext-enc-low-latency.enable", 1) } catch (_: Exception) {}
            try { setInteger("vendor.qti-ext-enc-slice-delivery-mode.enable", 1) } catch (_: Exception) {}

            // 5. Initial QP override: prevent initial bitrate/QP hunting on stream startup
            try {
                setInteger("vendor.qti-ext-enc-initial-qp.qp-i", 26)
                setInteger("vendor.qti-ext-enc-initial-qp.qp-p", 26)
            } catch (_: Exception) {}

            // ── Qualcomm tile-seam fix ────────────────────────────────────────
            // The Snapdragon encoder splits 640-wide frames into two 320px tiles
            // processed in parallel. The tile boundary at x=320 appears as a
            // visible vertical seam because the deblocking filter stops at tile edges.
            //
            // Force the encoder to treat the full frame as a single slice so:
            //   1. No vertical tile boundary exists in the output NAL units
            //   2. The deblocking loop filter runs across the full frame width
            try {
                // Standard key: slice height = full frame → single horizontal slice
                setInteger("slice-height", targetHeight)
            } catch (_: Exception) {}
            try {
                // Qualcomm OMX vendor extension: number of slices = 1
                setInteger("vendor.qti-ext-enc-multi-slice.num-slices", 1)
            } catch (_: Exception) {}
            try {
                // Disable Qualcomm's internal tiling/parallelism that causes the seam
                setInteger("vendor.qti-ext-enc-caps-ltr.max-count", 0)
            } catch (_: Exception) {}
        }

        val codec = createBestQualcommEncoder()
        codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
        inputSurface = codec.createInputSurface()
        codec.start()
        encoder = codec

        getEncoderExecutor().execute { drainEncoderLoop(codec) }
    }

    private fun openCameraAndStartCapture() {
        val cameraManager = getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameraId = getBackCameraId(cameraManager)

        if (ActivityCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            Log.e(TAG, "Camera permission not granted")
            stopStreaming()
            return
        }

        var retryCount = 0
        fun attemptOpen() {
            if (!isStreaming.get()) return
            try {
                cameraManager.openCamera(cameraId, object : CameraDevice.StateCallback() {
                    override fun onOpened(device: CameraDevice) {
                        if (!isStreaming.get()) {
                            try { device.close() } catch (_: Throwable) {}
                            return
                        }
                        cameraDevice = device
                        createCaptureSession(device)
                    }

                    override fun onDisconnected(device: CameraDevice) {
                        Log.w(TAG, "Camera disconnected")
                        try { device.close() } catch (_: Throwable) {}
                        if (cameraDevice == device) cameraDevice = null
                    }

                    override fun onError(device: CameraDevice, error: Int) {
                        Log.e(TAG, "Camera device error: $error")
                        try { device.close() } catch (_: Throwable) {}
                        if (cameraDevice == device) cameraDevice = null

                        if (isStreaming.get() && (error == ERROR_CAMERA_IN_USE || error == ERROR_MAX_CAMERAS_IN_USE) && retryCount < 3) {
                            retryCount++
                            Log.i(TAG, "Camera busy or closing, retrying in 300ms (attempt $retryCount)...")
                            getCameraHandler().postDelayed({ attemptOpen() }, 300)
                        } else {
                            stopStreaming()
                        }
                    }
                }, getCameraHandler())
            } catch (e: Exception) {
                Log.e(TAG, "Failed to open camera: ${e.message}", e)
                if (retryCount < 3) {
                    retryCount++
                    Log.i(TAG, "Retrying open camera in 300ms (attempt $retryCount)...")
                    getCameraHandler().postDelayed({ attemptOpen() }, 300)
                } else {
                    stopStreaming()
                }
            }
        }

        attemptOpen()
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

    /** Builds a repeating CaptureRequest for the given device, surface, and FPS range. */
    private fun buildCaptureRequest(device: CameraDevice, surface: Surface, fpsRange: Range<Int>): CaptureRequest {
        return device.createCaptureRequest(CameraDevice.TEMPLATE_RECORD).apply {
            addTarget(surface)

            set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_AUTO)

            // ── Manual Exposure Lock ──────────────────────────────────────────
            // Disabling AE removes two problems:
            //   1. Latency: AE selection can delay the frame by up to one full
            //      frame period (33ms at 30fps) while it converges.
            //   2. Quality: AE overexposes bright outdoor scenes (windows, sky)
            //      causing blown highlights that the H.264 encoder wastes bits on.
            //
            // Shutter 8ms = 1/125s — sharp for drone motion speeds, handles
            // normal daylight and indoor lighting.  ISO 800 = enough sensitivity
            // for indoor scenes without excessive noise.
            //
            // CONTROL_MODE stays AUTO so AWB and AF continue to work normally.
            set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
            set(CaptureRequest.SENSOR_EXPOSURE_TIME, 8_000_000L)       // 8ms in nanoseconds
            set(CaptureRequest.SENSOR_SENSITIVITY, 800)                 // ISO 800
            // Frame duration must be >= exposure time and match the target FPS
            val frameDurationNs = (1_000_000_000L / fpsRange.upper).coerceAtLeast(8_000_001L)
            set(CaptureRequest.SENSOR_FRAME_DURATION, frameDurationNs)

            set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE, CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
            set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE, CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)

            set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO)

            set(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_FAST)
            set(CaptureRequest.NOISE_REDUCTION_MODE, CameraMetadata.NOISE_REDUCTION_MODE_FAST)

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                try { set(CaptureRequest.DISTORTION_CORRECTION_MODE, CameraMetadata.DISTORTION_CORRECTION_MODE_OFF) } catch (_: Exception) {}
            }
            set(CaptureRequest.HOT_PIXEL_MODE, CameraMetadata.HOT_PIXEL_MODE_OFF)
            set(CaptureRequest.SHADING_MODE, CameraMetadata.SHADING_MODE_OFF)
            set(CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE, CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF)
            set(CaptureRequest.TONEMAP_MODE, CameraMetadata.TONEMAP_MODE_FAST)
        }.build()
    }

    private fun createCaptureSession(device: CameraDevice) {
        if (!isStreaming.get() || cameraDevice != device) return
        val encoderSurface = inputSurface
        if (encoderSurface == null || !encoderSurface.isValid) {
            Log.e(TAG, "Encoder surface is null or invalid")
            stopStreaming()
            return
        }

        try {
            val cameraManager = getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val fpsRange = getBestFpsRange(cameraManager, device.id, targetFps)
            Log.i(TAG, "Creating headless encoder capture session, AE FPS range: $fpsRange")

            device.createCaptureSession(listOf(encoderSurface), object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(session: CameraCaptureSession) {
                    if (!isStreaming.get() || cameraDevice != device) {
                        try { session.close() } catch (_: Throwable) {}
                        return
                    }
                    captureSession = session

                    try {
                        val request = buildCaptureRequest(device, encoderSurface, fpsRange)
                        session.setRepeatingRequest(request, object : CameraCaptureSession.CaptureCallback() {
                            override fun onCaptureFailed(session: CameraCaptureSession, request: CaptureRequest, failure: CaptureFailure) {
                                Log.w(TAG, "Frame capture failed: reason ${failure.reason}")
                            }
                        }, getCameraHandler())
                        Log.i(TAG, "Camera capture active (${targetFps}fps, Continuous AF)")
                    } catch (e: Exception) {
                        Log.e(TAG, "Failed to start repeating request", e)
                        stopStreaming()
                    }
                }

                override fun onConfigureFailed(session: CameraCaptureSession) {
                    Log.e(TAG, "Encoder capture session failed for ${targetWidth}x${targetHeight}")
                    try { session.close() } catch (_: Throwable) {}
                    stopStreaming()
                }
            }, getCameraHandler())
        } catch (e: Exception) {
            Log.e(TAG, "Failed to create capture session", e)
            stopStreaming()
        }
    }

    private fun drainEncoderLoop(codec: MediaCodec) {
        // AUDIO priority: real-time enough for encoding, won't peg CPU like URGENT_DISPLAY
        Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO)
        val bufferInfo = MediaCodec.BufferInfo()
        var sequence: Long = 0
        var bytesSentSinceReport: Long = 0
        var packetsSentSinceReport: Long = 0
        var framesSentSinceReport: Long = 0
        var lastStatsTime = System.currentTimeMillis()
        var lastPacketSentTime = System.currentTimeMillis()
        // 2ms dequeue timeout: wakes up within 2ms of frame ready without CPU spin
        val dequeueTimeoutUs = 2_000L

        while (isStreaming.get() && encoder == codec) {
            try {
                val outIndex = try {
                    codec.dequeueOutputBuffer(bufferInfo, dequeueTimeoutUs)
                } catch (e: IllegalStateException) {
                    break
                }
                val now = System.currentTimeMillis()

                if (outIndex >= 0) {
                    // Pure FPV: If multiple frames backed up in the encoder queue,
                    // drop stale older frames immediately so the cellular modem NEVER transmits old video!
                    var latestIndex = outIndex
                    var latestOffset = bufferInfo.offset
                    var latestSize = bufferInfo.size

                    while (true) {
                        val nextIndex = try {
                            codec.dequeueOutputBuffer(bufferInfo, 0L)
                        } catch (_: Exception) { -1 }

                        if (nextIndex >= 0) {
                            try { codec.releaseOutputBuffer(latestIndex, false) } catch (_: Exception) {}
                            latestIndex = nextIndex
                            latestOffset = bufferInfo.offset
                            latestSize = bufferInfo.size
                        } else {
                            break
                        }
                    }

                    val encodedBuffer = try {
                        codec.getOutputBuffer(latestIndex)
                    } catch (_: Exception) {
                        null
                    }
                    if (encodedBuffer != null && latestSize > 0) {
                        encodedBuffer.position(latestOffset)
                        encodedBuffer.limit(latestOffset + latestSize)

                        val pkts = sendDirectChunkedUdp(encodedBuffer, sequence)
                        packetsSentSinceReport += pkts
                        bytesSentSinceReport += latestSize
                        framesSentSinceReport += 1
                        sequence++
                        lastPacketSentTime = now
                    }
                    try {
                        codec.releaseOutputBuffer(latestIndex, false)
                    } catch (_: Exception) {}
                } else if (outIndex == MediaCodec.INFO_TRY_AGAIN_LATER) {
                    // Timed out with no frame — only send keep-alive every 80ms
                    // (15ms was hammering the 5G modem and keeping it at peak RF power)
                    if (now - lastPacketSentTime >= 80) {
                        sendCellularKeepAlive()
                        lastPacketSentTime = now
                    }
                    // No yield needed — blocking dequeue already gave CPU time back
                }

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
                if (isStreaming.get() && encoder == codec) {
                    Log.w(TAG, "Encoder poll exception: ${t.message}")
                }
                break
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

    private fun startUdpCommandListener(socket: DatagramSocket) {
        val exec = Executors.newSingleThreadExecutor()
        exec.execute {
            val buf = ByteArray(64)
            val packet = DatagramPacket(buf, buf.size)
            while (isStreaming.get() && udpSocket == socket) {
                try {
                    socket.receive(packet)
                    if (packet.length >= 2) {
                        // 0xFF 0x02: Keyframe request from laptop receiver
                        if (buf[0] == 0xFF.toByte() && buf[1] == 0x02.toByte()) {
                            Log.i(TAG, "Sync IDR frame requested by laptop receiver")
                            requestSyncFrame()
                        }
                    }
                } catch (_: Exception) {
                    break
                }
            }
        }
    }

    private fun requestSyncFrame() {
        try {
            val params = android.os.Bundle().apply {
                putInt(MediaCodec.PARAMETER_KEY_REQUEST_SYNC_FRAME, 0)
            }
            encoder?.setParameters(params)
        } catch (_: Exception) {}
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

            writeInt(videoPacketBuffer, 0, frameSeqInt)
            writeShort(videoPacketBuffer, 4, chunkIdx.toShort())
            writeShort(videoPacketBuffer, 6, totalChunks.toShort())
            writeLong(videoPacketBuffer, 8, timestampMs)

            buffer.get(videoPacketBuffer, headerSize, chunkSize)

            val packet = DatagramPacket(videoPacketBuffer, headerSize + chunkSize, target.address, target.port)
            try {
                socket.send(packet)
                packetsCount++
            } catch (_: Exception) {}

            // Micro-pacing for multi-chunk bursts (prevents LTE modem transport block queuing delay)
            if (totalChunks > 3 && (chunkIdx and 1 == 1)) {
                java.util.concurrent.locks.LockSupport.parkNanos(35_000L) // 35 microseconds
            }
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
        synchronized(stateLock) {
            stopStreamingInternal()
            stopSelf()
        }
    }

    private fun stopStreamingInternal() {
        if (!isStreaming.getAndSet(false)) return

        Log.i(TAG, "Stopping streaming pipeline...")

        sendControlPacket(PACKET_GOODBYE, 3)

        // 1. Close capture session
        try {
            captureSession?.stopRepeating()
            captureSession?.abortCaptures()
            captureSession?.close()
        } catch (_: Throwable) {}
        captureSession = null

        // 2. Close camera device
        try {
            cameraDevice?.close()
        } catch (_: Throwable) {}
        cameraDevice = null

        // 3. Stop hardware encoder and surface
        stopHardwareEncoder()

        // 4. Shutdown background executors
        try {
            encoderExecutor?.shutdownNow()
            encoderExecutor = null
        } catch (_: Throwable) {}

        // 5. Close UDP video socket
        try {
            udpSocket?.close()
        } catch (_: Throwable) {}
        udpSocket = null

        // 6. Stop telemetry bridge
        try {
            telemetryBridge?.stop()
        } catch (_: Throwable) {}
        telemetryBridge = null

        // 7. Release wakelock
        try {
            if (wakeLock?.isHeld == true) wakeLock?.release()
        } catch (_: Throwable) {}
        wakeLock = null

        // 8. Stop foreground notification
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION")
                stopForeground(true)
            }
        } catch (_: Throwable) {}

        // Broadcast stop status to UI
        try {
            val stoppedIntent = Intent(ACTION_STREAMING_STOPPED).apply {
                setPackage(packageName)
            }
            sendBroadcast(stoppedIntent)
        } catch (_: Exception) {}

        Log.i(TAG, "Streaming pipeline cleanly stopped and released")
    }

    override fun onTaskRemoved(rootIntent: Intent?) {
        sendControlPacket(PACKET_APP_CLOSE, 3)
        stopStreaming()
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        sendControlPacket(PACKET_APP_CLOSE, 3)
        stopStreaming()
        synchronized(stateLock) {
            try {
                cameraThread?.quitSafely()
                cameraThread?.join(500)
            } catch (_: Throwable) {}
            cameraThread = null
            cameraHandler = null
        }
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

package com.example.udpandroidapptransmitter

import android.Manifest
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.*
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : AppCompatActivity() {

    private lateinit var etTargetIp: EditText
    private lateinit var etTargetPort: EditText
    private lateinit var rgResolution: RadioGroup
    private lateinit var rbAuto: RadioButton
    private lateinit var rb480p: RadioButton
    private lateinit var rb360p: RadioButton
    private lateinit var tvResolutionLabel: TextView
    private lateinit var rgFps: RadioGroup
    private lateinit var rbFps60: RadioButton
    private lateinit var rbFps30: RadioButton
    private lateinit var tvFpsLabel: TextView
    private lateinit var tvBitrate: TextView
    private lateinit var tvFpsLive: TextView
    private lateinit var tvPackets: TextView
    private lateinit var tvStatusBadge: TextView
    private lateinit var tvStatusDetail: TextView
    private lateinit var viewStatusIndicator: View
    private lateinit var btnToggleStream: com.google.android.material.button.MaterialButton

    // Telemetry UI Elements
    private lateinit var cbEnableTelem: CheckBox
    private lateinit var etEspIp: EditText
    private lateinit var etTelemPort: EditText
    private lateinit var tvTelemUplink: TextView
    private lateinit var tvTelemDownlink: TextView
    private lateinit var tvTelemStatus: TextView

    private var isStreaming = false
    private val PREFS_NAME = "drone_streamer_prefs"

    private val statsReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            if (intent?.action == StreamingService.ACTION_STATS_BROADCAST) {
                val fps = intent.getFloatExtra(StreamingService.EXTRA_LIVE_FPS, 0.0f)
                val kbps = intent.getFloatExtra(StreamingService.EXTRA_LIVE_KBPS, 0.0f)
                val pkts = intent.getLongExtra(StreamingService.EXTRA_LIVE_PACKETS, 0L)

                val telemUp = intent.getLongExtra(StreamingService.EXTRA_TELEM_UPLINK, 0L)
                val telemDown = intent.getLongExtra(StreamingService.EXTRA_TELEM_DOWNLINK, 0L)
                val telemActive = intent.getBooleanExtra(StreamingService.EXTRA_TELEM_ACTIVE, false)

                tvFpsLive.text = "%.1f fps".format(fps)
                tvBitrate.text = "%.0f kbps".format(kbps)
                tvPackets.text = "$pkts pkts"

                tvTelemUplink.text = "$telemUp pkts"
                tvTelemDownlink.text = "$telemDown pkts"
                if (telemActive) {
                    if (telemUp > 0) {
                        tvTelemStatus.text = "LINK OK"
                        tvTelemStatus.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.neon_green))
                    } else {
                        tvTelemStatus.text = "SEARCHING"
                        tvTelemStatus.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.gold_bright))
                    }
                } else {
                    tvTelemStatus.text = "OFF"
                    tvTelemStatus.setTextColor(ContextCompat.getColor(this@MainActivity, R.color.text_dim))
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }
        setContentView(R.layout.activity_main)

        initViews()
        loadPreferences()
        checkPermissions()
    }

    private fun initViews() {
        etTargetIp = findViewById(R.id.et_target_ip)
        etTargetPort = findViewById(R.id.et_target_port)
        rgResolution = findViewById(R.id.rg_resolution)
        rbAuto = findViewById(R.id.rb_auto)
        rb480p = findViewById(R.id.rb_480p)
        rb360p = findViewById(R.id.rb_360p)
        tvResolutionLabel = findViewById(R.id.tv_resolution_label)

        rgFps = findViewById(R.id.rg_fps)
        rbFps60 = findViewById(R.id.rb_fps_60)
        rbFps30 = findViewById(R.id.rb_fps_30)
        tvFpsLabel = findViewById(R.id.tv_fps_label)

        tvBitrate = findViewById(R.id.tv_bitrate)
        tvFpsLive = findViewById(R.id.tv_fps_live)
        tvPackets = findViewById(R.id.tv_packets)
        tvStatusBadge = findViewById(R.id.tv_status_badge)
        tvStatusDetail = findViewById(R.id.tv_status_detail)
        viewStatusIndicator = findViewById(R.id.view_status_indicator)
        btnToggleStream = findViewById(R.id.btn_toggle_stream)

        cbEnableTelem = findViewById(R.id.cb_enable_telem)
        etEspIp = findViewById(R.id.et_esp_ip)
        etTelemPort = findViewById(R.id.et_telem_port)
        tvTelemUplink = findViewById(R.id.tv_telem_uplink)
        tvTelemDownlink = findViewById(R.id.tv_telem_downlink)
        tvTelemStatus = findViewById(R.id.tv_telem_status)

        // Quality / Resolution selection listener
        rgResolution.setOnCheckedChangeListener { _, checkedId ->
            when (checkedId) {
                R.id.rb_auto -> {
                    tvResolutionLabel.text = "QUALITY: AUTO (L < 150ms)"
                    tvResolutionLabel.setTextColor(ContextCompat.getColor(this, R.color.gold_bright))
                }
                R.id.rb_480p -> {
                    tvResolutionLabel.text = "QUALITY: 480p FIXED (640x480)"
                    tvResolutionLabel.setTextColor(ContextCompat.getColor(this, R.color.white_pure))
                }
                R.id.rb_360p -> {
                    tvResolutionLabel.text = "QUALITY: 360p FIXED (640x360)"
                    tvResolutionLabel.setTextColor(ContextCompat.getColor(this, R.color.white_pure))
                }
            }
            updateIdleStatusDetail()
            saveCurrentPreferences()
        }

        // FPS selection listener
        rgFps.setOnCheckedChangeListener { _, checkedId ->
            when (checkedId) {
                R.id.rb_fps_60 -> {
                    tvFpsLabel.text = "TARGET FPS: 60 (16ms)"
                    tvFpsLabel.setTextColor(ContextCompat.getColor(this, R.color.gold_bright))
                }
                R.id.rb_fps_30 -> {
                    tvFpsLabel.text = "TARGET FPS: 30 (33ms)"
                    tvFpsLabel.setTextColor(ContextCompat.getColor(this, R.color.white_pure))
                }
            }
            updateIdleStatusDetail()
            saveCurrentPreferences()
        }

        // Telemetry toggle listener
        cbEnableTelem.setOnCheckedChangeListener { _, isChecked ->
            etEspIp.isEnabled = isChecked
            etTelemPort.isEnabled = isChecked
            etEspIp.alpha = if (isChecked) 1.0f else 0.45f
            etTelemPort.alpha = if (isChecked) 1.0f else 0.45f
            if (isChecked) {
                tvTelemStatus.text = "READY"
                tvTelemStatus.setTextColor(ContextCompat.getColor(this, R.color.gold_bright))
            } else {
                tvTelemStatus.text = "OFF"
                tvTelemStatus.setTextColor(ContextCompat.getColor(this, R.color.text_dim))
            }
            saveCurrentPreferences()
        }

        btnToggleStream.setOnClickListener {
            if (isStreaming) {
                stopStream()
            } else {
                startStream()
            }
        }
    }

    private fun setConfigControlsEnabled(enabled: Boolean) {
        etTargetIp.isEnabled = enabled
        etTargetPort.isEnabled = enabled
        rbAuto.isEnabled = enabled
        rb480p.isEnabled = enabled
        rb360p.isEnabled = enabled
        rbFps60.isEnabled = enabled
        rbFps30.isEnabled = enabled
        cbEnableTelem.isEnabled = enabled

        if (enabled) {
            val telemEnabled = cbEnableTelem.isChecked
            etEspIp.isEnabled = telemEnabled
            etTelemPort.isEnabled = telemEnabled
            etEspIp.alpha = if (telemEnabled) 1.0f else 0.45f
            etTelemPort.alpha = if (telemEnabled) 1.0f else 0.45f
        } else {
            etEspIp.isEnabled = false
            etTelemPort.isEnabled = false
            etEspIp.alpha = 0.45f
            etTelemPort.alpha = 0.45f
        }
    }

    private fun updateIdleStatusDetail() {
        if (!isStreaming) {
            val qualityStr = when {
                rbAuto.isChecked -> "Auto Quality (640x480 adaptive)"
                rb480p.isChecked -> "Fixed 480p (640x480)"
                else -> "Fixed 360p (640x360)"
            }
            val fpsStr = if (rbFps60.isChecked) "60fps" else "30fps"
            val ip = etTargetIp.text.toString().trim().ifEmpty { "192.168.191.187" }
            val port = etTargetPort.text.toString().trim().ifEmpty { "5005" }
            tvStatusDetail.text = "Direct Qualcomm AVC pipe ready • Configured for $qualityStr @ $fpsStr to $ip:$port"
        }
    }

    private fun startStream() {
        val ip = etTargetIp.text.toString().trim()
        val port = etTargetPort.text.toString().trim().toIntOrNull() ?: 5005
        val espIp = etEspIp.text.toString().trim()
        val telemPort = etTelemPort.text.toString().trim().toIntOrNull() ?: 14551
        val enableTelem = cbEnableTelem.isChecked

        if (ip.isEmpty()) {
            Toast.makeText(this, "Please enter target IP", Toast.LENGTH_SHORT).show()
            return
        }

        saveCurrentPreferences()

        val fps = if (rbFps60.isChecked) 60 else 30
        val isAuto = rbAuto.isChecked

        val (width, height, bitrate) = when {
            isAuto -> {
                // Auto mode starts with balanced 480p @ 600 kbps, dynamically adapts based on latency
                val br = if (fps == 60) 700_000 else 500_000
                Triple(640, 480, br)
            }
            rb360p.isChecked -> {
                val br = if (fps == 60) 450_000 else 300_000
                Triple(640, 360, br)
            }
            else -> {
                val br = if (fps == 60) 1_500_000 else 1_000_000
                Triple(640, 480, br)
            }
        }

        val serviceIntent = Intent(this, StreamingService::class.java).apply {
            action = StreamingService.ACTION_START
            putExtra(StreamingService.EXTRA_TARGET_IP, ip)
            putExtra(StreamingService.EXTRA_TARGET_PORT, port)
            putExtra(StreamingService.EXTRA_WIDTH, width)
            putExtra(StreamingService.EXTRA_HEIGHT, height)
            putExtra(StreamingService.EXTRA_FPS, fps)
            putExtra(StreamingService.EXTRA_BITRATE, bitrate)
            putExtra(StreamingService.EXTRA_AUTO_QUALITY, isAuto)
            putExtra(StreamingService.EXTRA_ENABLE_TELEMETRY, enableTelem)
            putExtra(StreamingService.EXTRA_ESP_IP, espIp)
            putExtra(StreamingService.EXTRA_TELEMETRY_PORT, telemPort)
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }

        isStreaming = true
        setConfigControlsEnabled(false)

        btnToggleStream.text = "ABORT // STOP STREAMING"
        btnToggleStream.strokeColor = ContextCompat.getColorStateList(this, R.color.neon_red)
        btnToggleStream.setTextColor(ContextCompat.getColor(this, R.color.neon_red))
        btnToggleStream.backgroundTintList = ContextCompat.getColorStateList(this, R.color.black_carbon)

        tvStatusBadge.text = "TRANSMITTING LIVE"
        tvStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.neon_green))
        viewStatusIndicator.setBackgroundResource(R.drawable.dot_neon_green)
        val modeDesc = if (isAuto) "Auto Quality" else "${width}x${height}"
        tvStatusDetail.text = "Direct Qualcomm AVC pipe active • $modeDesc @ ${fps}fps to $ip:$port"
    }

    private fun stopStream() {
        val serviceIntent = Intent(this, StreamingService::class.java).apply {
            action = StreamingService.ACTION_STOP
        }
        startService(serviceIntent)

        isStreaming = false
        setConfigControlsEnabled(true)

        btnToggleStream.text = "ENGAGE // START STREAMING"
        btnToggleStream.strokeColor = ContextCompat.getColorStateList(this, R.color.gold_bright)
        btnToggleStream.setTextColor(ContextCompat.getColor(this, R.color.white_pure))
        btnToggleStream.backgroundTintList = ContextCompat.getColorStateList(this, R.color.black_carbon)

        tvStatusBadge.text = "TRANSMITTER IDLE"
        tvStatusBadge.setTextColor(ContextCompat.getColor(this, R.color.gold_bright))
        viewStatusIndicator.setBackgroundResource(R.drawable.dot_neon_idle)
        updateIdleStatusDetail()

        tvFpsLive.text = "0.0 fps"
        tvBitrate.text = "0 kbps"
    }

    private fun checkPermissions() {
        val permissions = mutableListOf(Manifest.permission.CAMERA)
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.S) {
            permissions.add(Manifest.permission.BLUETOOTH_CONNECT)
            permissions.add(Manifest.permission.BLUETOOTH_SCAN)
        }
        val missing = permissions.filter {
            ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1001)
        }
    }

    private fun loadPreferences() {
        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        etTargetIp.setText(prefs.getString("target_ip", "192.168.191.187"))
        etTargetPort.setText(prefs.getInt("target_port", 5005).toString())
        etEspIp.setText(prefs.getString("esp_ip", "APM-Bridge"))
        etTelemPort.setText(prefs.getInt("telemetry_port", 14551).toString())

        val enableTelem = prefs.getBoolean("enable_telemetry", true)
        cbEnableTelem.isChecked = enableTelem
        etEspIp.isEnabled = enableTelem
        etTelemPort.isEnabled = enableTelem
        etEspIp.alpha = if (enableTelem) 1.0f else 0.45f
        etTelemPort.alpha = if (enableTelem) 1.0f else 0.45f

        val quality = prefs.getString("selected_quality", "auto")
        when (quality) {
            "480p" -> rb480p.isChecked = true
            "360p" -> rb360p.isChecked = true
            else -> rbAuto.isChecked = true
        }

        val fps = prefs.getInt("selected_fps", 60)
        if (fps == 30) {
            rbFps30.isChecked = true
        } else {
            rbFps60.isChecked = true
        }

        updateIdleStatusDetail()
    }

    private fun saveCurrentPreferences() {
        val ip = etTargetIp.text.toString().trim()
        val port = etTargetPort.text.toString().trim().toIntOrNull() ?: 5005
        val espIp = etEspIp.text.toString().trim()
        val telemPort = etTelemPort.text.toString().trim().toIntOrNull() ?: 14551
        val enableTelem = cbEnableTelem.isChecked
        val quality = when {
            rb480p.isChecked -> "480p"
            rb360p.isChecked -> "360p"
            else -> "auto"
        }
        val fps = if (rbFps30.isChecked) 30 else 60

        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit()
            .putString("target_ip", ip)
            .putInt("target_port", port)
            .putString("esp_ip", espIp)
            .putInt("telemetry_port", telemPort)
            .putBoolean("enable_telemetry", enableTelem)
            .putString("selected_quality", quality)
            .putInt("selected_fps", fps)
            .apply()
    }

    override fun onResume() {
        super.onResume()
        val filter = IntentFilter(StreamingService.ACTION_STATS_BROADCAST)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(statsReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(statsReceiver, filter)
        }
    }

    override fun onPause() {
        super.onPause()
        try {
            unregisterReceiver(statsReceiver)
        } catch (_: Exception) {}
    }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (isStreaming) {
            Toast.makeText(this, "Stopping stream...", Toast.LENGTH_SHORT).show()
            stopStream()
        }
        super.onBackPressed()
    }

    override fun onDestroy() {
        if (isStreaming) {
            stopStream()
        }
        super.onDestroy()
    }
}
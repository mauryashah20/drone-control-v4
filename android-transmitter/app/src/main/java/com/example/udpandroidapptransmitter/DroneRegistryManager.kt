package com.example.udpandroidapptransmitter

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.os.Handler
import android.os.Looper
import android.util.Log
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.Inet6Address
import java.net.NetworkInterface
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

object DroneRegistryManager {

    private const val TAG = "DroneRegistry"
    private val executor = Executors.newSingleThreadExecutor()
    private val mainHandler = Handler(Looper.getMainLooper())

    const val DEFAULT_REGISTRY_URL = "https://drone-registry-one.vercel.app"
    const val DEFAULT_DRONE_ID = "DRONE-001"
    const val DEFAULT_DEVICE_TOKEN = "acfd09978673d60a0be1121ee701805cdc275f14a1849e44"

    @Volatile var lastRegisteredIp: String? = null
        private set
    @Volatile var tempLocalIp: String? = null
        private set
    @Volatile var lastDiscoveredTargetIp: String? = null
    @Volatile var lastDiscoveredTargetPort: Int = 5005
    var onTargetDiscovered: ((String, Int) -> Unit)? = null

    @Volatile var isAutoSyncRunning = false
        private set

    private var scheduledExecutor: ScheduledExecutorService? = null
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    /**
     * Inspects active network interfaces and extracts the globally routable IPv6 address
     * (e.g. Jio/Airtel cellular 2401:... or standard 2xxx:... global unicast).
     */
    fun detectCellularIPv6(): String? {
        try {
            val interfaces = NetworkInterface.getNetworkInterfaces() ?: return null
            val candidates = mutableListOf<Pair<String, String>>()

            for (iface in interfaces) {
                if (!iface.isUp || iface.isLoopback) continue
                val addrs = iface.inetAddresses
                for (addr in addrs) {
                    if (addr is Inet6Address) {
                        val host = addr.hostAddress?.split("%")?.get(0)?.trim() ?: continue
                        val lower = host.lowercase()

                        // Reject loopback, link-local (fe80::), ULA (fc00::/7)
                        if (lower == "::1" || lower == "::") continue
                        if (lower.startsWith("fe8") || lower.startsWith("fe9") || lower.startsWith("fea") || lower.startsWith("feb")) continue
                        if (lower.startsWith("fc") || lower.startsWith("fd") || lower.startsWith("ff")) continue

                        // Must be global unicast
                        if (lower.startsWith("2")) {
                            candidates.add(Pair(iface.name, host))
                        }
                    }
                }
            }

            // Prioritize cellular modem interface names (rmnet, ccmni, pdp, wwan, usb)
            val cellularPrefixes = listOf("rmnet", "ccmni", "pdp", "wwan", "usb")
            for (prefix in cellularPrefixes) {
                for ((name, ip) in candidates) {
                    if (name.lowercase().contains(prefix)) {
                        Log.i(TAG, "Detected cellular IPv6 on $name: $ip")
                        return ip
                    }
                }
            }

            // Fallback to any non-loopback global IPv6 (e.g. Wi-Fi IPv6)
            if (candidates.isNotEmpty()) {
                val (name, ip) = candidates.first()
                Log.i(TAG, "Detected global IPv6 on $name: $ip")
                return ip
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error detecting local IPv6: ${e.message}")
        }
        return null
    }

    /**
     * Registers the drone's IPv6 with the Vercel discovery API asynchronously.
     */
    fun registerDrone(
        registryUrl: String = DEFAULT_REGISTRY_URL,
        deviceId: String = DEFAULT_DRONE_ID,
        token: String = DEFAULT_DEVICE_TOKEN,
        port: Int = 5005,
        onResult: (success: Boolean, message: String, detectedIp: String?) -> Unit
    ) {
        executor.execute {
            val detectedIp = detectCellularIPv6()
            if (detectedIp == null) {
                mainHandler.post {
                    onResult(false, "No global IPv6 found on device interfaces", null)
                }
                return@execute
            }

            try {
                val url = URL("${registryUrl.trimEnd('/')}/api/register")
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "application/json")
                conn.setRequestProperty("Authorization", "Bearer $token")
                conn.connectTimeout = 8000
                conn.readTimeout = 8000
                conn.doOutput = true

                val payload = JSONObject().apply {
                    put("deviceId", deviceId)
                    put("ipv6", detectedIp)
                    put("port", port)
                    put("token", token)
                }

                OutputStreamWriter(conn.outputStream).use { it.write(payload.toString()) }

                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val responseText = BufferedReader(InputStreamReader(stream)).use { it.readText() }
                conn.disconnect()

                val json = JSONObject(responseText)
                val success = json.optBoolean("success", false)
                val msg = if (success) {
                    lastRegisteredIp = detectedIp
                    tempLocalIp = detectedIp

                    // Check if counterpart peer device (Ground Station) is returned
                    val peerObj = json.optJSONObject("peerTarget")
                    if (peerObj != null) {
                        val peerIp = peerObj.optString("ipv6")
                        val peerPort = peerObj.optInt("port", 5005)
                        if (peerIp.isNotEmpty()) {
                            lastDiscoveredTargetIp = peerIp
                            lastDiscoveredTargetPort = peerPort
                            Log.i(TAG, "Vercel register response included peer target: [$peerIp]:$peerPort")
                            StreamingService.activeInstance?.updateTargetAddress(peerIp, peerPort)
                            mainHandler.post { onTargetDiscovered?.invoke(peerIp, peerPort) }
                        }
                    }

                    "Registered $deviceId at [$detectedIp]:$port"
                } else {
                    json.optString("error", "HTTP $code")
                }

                mainHandler.post {
                    onResult(success, msg, detectedIp)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Registration error: ${e.message}", e)
                mainHandler.post {
                    onResult(false, "Network error: ${e.message}", detectedIp)
                }
            }
        }
    }

    /**
     * Queries the Vercel registry for the target device's latest IPv6 address (e.g. Ground Station).
     */
    fun lookupTarget(
        registryUrl: String = DEFAULT_REGISTRY_URL,
        deviceId: String = "GROUND-001",
        onResult: (success: Boolean, ipv6: String?, port: Int?, isOnline: Boolean, message: String) -> Unit
    ) {
        executor.execute {
            try {
                val url = URL("${registryUrl.trimEnd('/')}/api/lookup?deviceId=$deviceId")
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "GET"
                conn.connectTimeout = 6000
                conn.readTimeout = 6000

                val code = conn.responseCode
                val stream = if (code in 200..299) conn.inputStream else conn.errorStream
                val responseText = BufferedReader(InputStreamReader(stream)).use { it.readText() }
                conn.disconnect()

                if (code == 200) {
                    val json = JSONObject(responseText)
                    val ipv6 = json.optString("ipv6")
                    val port = json.optInt("port", 5005)
                    val isOnline = json.optBoolean("isOnline", false)
                    val age = json.optInt("secondsSinceLastSeen", 0)

                    val statusMsg = if (isOnline) {
                        "Target $deviceId online ($age sec ago)"
                    } else {
                        "Target $deviceId OFFLINE (last seen $age sec ago)"
                    }

                    mainHandler.post {
                        onResult(true, ipv6, port, isOnline, statusMsg)
                    }
                } else {
                    mainHandler.post {
                        onResult(false, null, null, false, "Device $deviceId not found (HTTP $code)")
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Lookup error: ${e.message}", e)
                mainHandler.post {
                    onResult(false, null, null, false, "Lookup error: ${e.message}")
                }
            }
        }
    }

    /**
     * Lightweight heartbeat sent periodically while streaming.
     */
    fun sendHeartbeat(
        registryUrl: String = DEFAULT_REGISTRY_URL,
        deviceId: String = DEFAULT_DRONE_ID,
        token: String = DEFAULT_DEVICE_TOKEN
    ) {
        executor.execute {
            try {
                val url = URL("${registryUrl.trimEnd('/')}/api/heartbeat")
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "application/json")
                conn.setRequestProperty("Authorization", "Bearer $token")
                conn.connectTimeout = 5000
                conn.readTimeout = 5000
                conn.doOutput = true

                val payload = JSONObject().apply {
                    put("deviceId", deviceId)
                    put("token", token)
                }

                OutputStreamWriter(conn.outputStream).use { it.write(payload.toString()) }
                conn.responseCode
                conn.disconnect()
            } catch (e: Exception) {
                Log.w(TAG, "Heartbeat failed: ${e.message}")
            }
        }
    }

    @Volatile var lastTargetResponseTimeMs: Long = 0L
    @Volatile var lastLookupTimeMs: Long = 0L

    fun markTargetResponded() {
        lastTargetResponseTimeMs = System.currentTimeMillis()
    }

    /**
     * Rule 2: If the targeted IP is not responding for more than 10 seconds, only then look up.
     * Enforces a 10s cooldown to prevent Vercel request quota exhaustion.
     */
    fun lookupTargetIfUnresponsive(
        targetDeviceId: String = "GROUND-001",
        unresponsiveThresholdMs: Long = 10_000L,
        onResult: (success: Boolean, ipv6: String?, port: Int?, isOnline: Boolean, message: String) -> Unit
    ) {
        val now = System.currentTimeMillis()
        if (lastTargetResponseTimeMs > 0 && (now - lastTargetResponseTimeMs < unresponsiveThresholdMs)) {
            // Target is currently responding, do NOT query Vercel!
            return
        }

        if (now - lastLookupTimeMs < unresponsiveThresholdMs) {
            // In cooldown, avoid spamming Vercel
            return
        }

        lastLookupTimeMs = now
        Log.i(TAG, "Target $targetDeviceId unresponsive (> ${unresponsiveThresholdMs / 1000}s) — querying Vercel discovery...")
        lookupTarget(deviceId = targetDeviceId, onResult = onResult)
    }

    // ─────────────────────────────────────────────────────────────────────────
    // ZERO-TOUCH IPV6 DISCOVERY & ROAMING DAEMON
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Starts automatic IPv6 discovery, registration, and roaming monitoring.
     * Rule 1: Registers once on initialization. Re-registers ONLY when phone's own cellular IPv6 changes.
     * ZERO recurring heartbeats — preserves Vercel free quota.
     */
    fun startAutoSync(
        context: Context,
        port: Int = 5005,
        onStatusUpdate: ((message: String, isOnline: Boolean) -> Unit)? = null
    ) {
        if (isAutoSyncRunning) return
        isAutoSyncRunning = true

        // 1. Initial immediate check & registration on app initialization
        checkAndUpdateIpv6(port, onStatusUpdate)

        // 2. Start internal check loop every 750ms (pure internal inspection, 0 network requests when unchanged)
        try {
            val sched = Executors.newSingleThreadScheduledExecutor()
            scheduledExecutor = sched
            sched.scheduleWithFixedDelay({
                try {
                    val currentIp = detectCellularIPv6()
                    if (currentIp != null && currentIp != tempLocalIp) {
                        Log.i(TAG, "Internal IP check (every 750ms): Detected change [$tempLocalIp] -> [$currentIp]. Replacing temp IP and updating Vercel...")
                        tempLocalIp = currentIp
                        registerDrone(port = port) { success, msg, ip ->
                            if (success && ip != null) {
                                lastRegisteredIp = ip
                                mainHandler.post { onStatusUpdate?.invoke("Drone registered on Vercel: [$ip]:$port", true) }
                            } else {
                                mainHandler.post { onStatusUpdate?.invoke("Auto-sync error: $msg", false) }
                            }
                        }
                    }
                } catch (e: Exception) {
                    Log.w(TAG, "Internal IP check loop error: ${e.message}")
                }
            }, 750, 750, TimeUnit.MILLISECONDS)
        } catch (e: Exception) {
            Log.w(TAG, "Could not start scheduled IP check loop: ${e.message}")
        }

        // 3. Register Android NetworkCallback for OS carrier network change events
        try {
            val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            if (cm != null) {
                val request = NetworkRequest.Builder()
                    .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                    .build()

                val cb = object : ConnectivityManager.NetworkCallback() {
                    override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
                        checkAndUpdateIpv6(port, onStatusUpdate)
                    }

                    override fun onAvailable(network: Network) {
                        checkAndUpdateIpv6(port, onStatusUpdate)
                    }
                }
                cm.registerNetworkCallback(request, cb)
                networkCallback = cb
            }
        } catch (e: Exception) {
            Log.w(TAG, "Could not register NetworkCallback: ${e.message}")
        }

        Log.i(TAG, "Zero-touch AutoSync active (750ms internal check + cellular change monitor)")
    }

    /**
     * Checks if device IPv6 has changed; if changed, registers new IP with Vercel.
     * If unchanged, makes ZERO network requests to preserve Vercel quota.
     */
    fun checkAndUpdateIpv6(
        port: Int = 5005,
        onStatusUpdate: ((message: String, isOnline: Boolean) -> Unit)? = null
    ) {
        executor.execute {
            val currentIp = detectCellularIPv6()
            if (currentIp == null) {
                mainHandler.post { onStatusUpdate?.invoke("Searching for cellular IPv6...", false) }
                return@execute
            }

            if (currentIp != tempLocalIp) {
                Log.i(TAG, "AutoSync: New carrier IPv6 detected [$currentIp] (was [$tempLocalIp]) -> registering...")
                tempLocalIp = currentIp
                registerDrone(port = port) { success, msg, ip ->
                    if (success && ip != null) {
                        lastRegisteredIp = ip
                        mainHandler.post { onStatusUpdate?.invoke("Drone registered on Vercel: [$ip]:$port", true) }
                    } else {
                        mainHandler.post { onStatusUpdate?.invoke("Auto-sync error: $msg", false) }
                    }
                }
            } else {
                // IP is current — do NOT send heartbeat, avoid burning Vercel request quota
                mainHandler.post { onStatusUpdate?.invoke("Drone registered on Vercel: [$currentIp]:$port", true) }
            }
        }
    }

    /**
     * Stops the background auto-sync daemon and unregisters system callbacks.
     */
    fun stopAutoSync(context: Context) {
        isAutoSyncRunning = false
        try {
            networkCallback?.let {
                val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
                cm?.unregisterNetworkCallback(it)
            }
            networkCallback = null
        } catch (_: Exception) {}

        try {
            scheduledExecutor?.shutdownNow()
            scheduledExecutor = null
        } catch (_: Exception) {}
        Log.i(TAG, "AutoSync daemon stopped")
    }
}

import type { VercelRequest, VercelResponse } from "@vercel/node";
import { validateDeviceId } from "../lib/validate";
import { getDevice } from "../lib/db";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, x-device-token, x-registration-secret, x-admin-key");

  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  // Allow GET
  if (req.method !== "GET") {
    res.setHeader("Allow", ["GET", "OPTIONS"]);
    return res.status(405).json({ success: false, error: `Method ${req.method} Not Allowed` });
  }

  try {
    const rawDeviceId = req.query.deviceId || req.query.id;
    const deviceId = typeof rawDeviceId === "string" 
      ? rawDeviceId.trim().toUpperCase().replace(/\s+/g, "") 
      : (Array.isArray(rawDeviceId) && typeof rawDeviceId[0] === "string" ? rawDeviceId[0].trim().toUpperCase().replace(/\s+/g, "") : "");

    if (!validateDeviceId(deviceId)) {
      return res.status(400).json({
        success: false,
        error: "Invalid or missing deviceId query parameter (must be 1-64 alphanumeric/dash/underscore)",
      });
    }

    const device = await getDevice(deviceId);

    if (!device) {
      return res.status(404).json({
        success: false,
        error: `Device '${deviceId}' not found in registry`,
      });
    }

    const thresholdSec = parseInt(process.env.ONLINE_THRESHOLD_SECONDS || "60", 10);
    const lastSeenDate = device.last_seen ? new Date(device.last_seen) : new Date();
    const lastSeenMs = isNaN(lastSeenDate.getTime()) ? Date.now() : lastSeenDate.getTime();
    const nowMs = Date.now();
    const secondsSinceLastSeen = Math.max(0, Math.floor((nowMs - lastSeenMs) / 1000));
    const isOnline = secondsSinceLastSeen <= thresholdSec;

    const updatedAtDate = device.updated_at ? new Date(device.updated_at) : lastSeenDate;

    // Cache control: Ground stations need fresh discovery data, cache for at most 3 seconds
    res.setHeader("Cache-Control", "public, s-maxage=3, max-age=3, stale-while-revalidate=5");

    return res.status(200).json({
      success: true,
      deviceId: device.device_id,
      ipv6: device.ipv6,
      port: device.port,
      lastSeen: isNaN(lastSeenDate.getTime()) ? new Date().toISOString() : lastSeenDate.toISOString(),
      updatedAt: isNaN(updatedAtDate.getTime()) ? new Date().toISOString() : updatedAtDate.toISOString(),
      isOnline,
      secondsSinceLastSeen,
      onlineThresholdSeconds: thresholdSec,
    });
  } catch (error: any) {
    console.error("[lookup] Internal error:", error);
    return res.status(500).json({
      success: false,
      error: "Internal server error",
      details: process.env.NODE_ENV === "development" ? error?.message : undefined,
    });
  }
}

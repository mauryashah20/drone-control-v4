import type { VercelRequest, VercelResponse } from "@vercel/node";
import { validateDeviceId, validateIPv6, validatePort } from "../lib/validate";
import { verifyToken, extractBearerToken } from "../lib/auth";
import { isGlobalUnicastIPv6 } from "../lib/ip";
import { getDevice, updateHeartbeat } from "../lib/db";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method !== "POST") {
    res.setHeader("Allow", ["POST"]);
    return res.status(405).json({ success: false, error: `Method ${req.method} Not Allowed` });
  }

  try {
    const rawDeviceId = req.body?.deviceId;
    const deviceId = typeof rawDeviceId === "string" ? rawDeviceId.trim().toUpperCase().replace(/\s+/g, "") : "";
    const { ipv6, port, token } = req.body || {};

    if (!validateDeviceId(deviceId)) {
      return res.status(400).json({
        success: false,
        error: "Invalid deviceId format",
      });
    }

    const bearer = extractBearerToken(req);
    const providedToken = bearer || token || (req.headers["x-device-token"] as string);

    if (!providedToken) {
      return res.status(401).json({
        success: false,
        error: "Missing authorization token",
      });
    }

    const existingDevice = await getDevice(deviceId);
    if (!existingDevice) {
      return res.status(404).json({
        success: false,
        error: `Device '${deviceId}' not found. Please register first.`,
      });
    }

    if (!verifyToken(deviceId, providedToken, existingDevice.token_hash)) {
      return res.status(401).json({
        success: false,
        error: "Unauthorized: invalid device token",
      });
    }

    let updatedIpv6: string | undefined = undefined;
    let updatedPort: number | undefined = undefined;

    if (ipv6 !== undefined) {
      if (!validateIPv6(ipv6) || !isGlobalUnicastIPv6(ipv6)) {
        return res.status(400).json({
          success: false,
          error: "Invalid or non-global ipv6 provided in heartbeat",
        });
      }
      updatedIpv6 = ipv6;
    }

    if (port !== undefined) {
      if (!validatePort(port)) {
        return res.status(400).json({
          success: false,
          error: "Invalid port number provided in heartbeat",
        });
      }
      updatedPort = port;
    }

    const updated = await updateHeartbeat(deviceId, updatedIpv6, updatedPort);

    return res.status(200).json({
      success: true,
      deviceId: updated?.device_id || deviceId,
      ipv6: updated?.ipv6 || existingDevice.ipv6,
      port: updated?.port || existingDevice.port,
      lastSeen: updated?.last_seen || new Date().toISOString(),
      isOnline: true,
      message: "Heartbeat acknowledged",
    });
  } catch (error: any) {
    console.error("[heartbeat] Internal error:", error);
    return res.status(500).json({
      success: false,
      error: "Internal server error",
      details: process.env.NODE_ENV === "development" ? error?.message : undefined,
    });
  }
}

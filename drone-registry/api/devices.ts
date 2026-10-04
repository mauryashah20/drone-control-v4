import type { VercelRequest, VercelResponse } from "@vercel/node";
import { verifyAdmin, extractBearerToken, verifyToken } from "../lib/auth";
import { validateDeviceId } from "../lib/validate";
import { listAllDevices, getDevice, deleteDevice } from "../lib/db";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  if (req.method === "GET") {
    // List of all devices for registry dashboard & status monitoring
    const devices = await listAllDevices();
    const thresholdSec = parseInt(process.env.ONLINE_THRESHOLD_SECONDS || "60", 10);
    const nowMs = Date.now();

    const formatted = devices.map((d) => {
      const diffSec = Math.max(0, Math.floor((nowMs - new Date(d.last_seen).getTime()) / 1000));
      return {
        ...d,
        isOnline: diffSec <= thresholdSec,
        secondsSinceLastSeen: diffSec,
      };
    });

    return res.status(200).json({ success: true, count: formatted.length, devices: formatted });
  }

  if (req.method === "DELETE") {
    const rawDeviceId = req.query.deviceId || req.body?.deviceId;
    const deviceId = typeof rawDeviceId === "string" 
      ? rawDeviceId.trim().toUpperCase().replace(/\s+/g, "") 
      : (Array.isArray(rawDeviceId) && typeof rawDeviceId[0] === "string" ? rawDeviceId[0].trim().toUpperCase().replace(/\s+/g, "") : "");

    if (!validateDeviceId(deviceId)) {
      return res.status(400).json({ success: false, error: "Invalid deviceId" });
    }

    const device = await getDevice(deviceId);
    if (!device) {
      return res.status(404).json({ success: false, error: "Device not found" });
    }

    const bearer = extractBearerToken(req) || req.body?.token;
    const isAdmin = verifyAdmin(req);
    const isDeviceOwner = bearer && verifyToken(deviceId, bearer, device.token_hash);

    if (!isAdmin && !isDeviceOwner) {
      return res.status(401).json({ success: false, error: "Unauthorized: valid token or admin secret required" });
    }

    await deleteDevice(deviceId);
    return res.status(200).json({ success: true, message: `Device '${deviceId}' unregistered successfully` });
  }

  res.setHeader("Allow", ["GET", "DELETE"]);
  return res.status(405).json({ success: false, error: `Method ${req.method} Not Allowed` });
}

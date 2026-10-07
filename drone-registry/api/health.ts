import type { VercelRequest, VercelResponse } from "@vercel/node";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "GET, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, x-device-token, x-registration-secret, x-admin-key");

  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  if (req.method !== "GET") {
    res.setHeader("Allow", ["GET", "OPTIONS"]);
    return res.status(405).json({ success: false, error: "Method Not Allowed" });
  }

  return res.status(200).json({
    status: "ok",
    service: "drone-ipv6-registry",
    timestamp: new Date().toISOString(),
    version: "1.0.0",
    docs: {
      register: "POST /api/register",
      lookup: "GET /api/lookup?deviceId=:id",
      heartbeat: "POST /api/heartbeat",
      devices: "GET /api/devices (Admin)",
    },
  });
}

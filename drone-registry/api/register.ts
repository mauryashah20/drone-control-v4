import type { VercelRequest, VercelResponse } from "@vercel/node";
import { validateDeviceId, validateIPv6, validatePort } from "../lib/validate";
import { hashToken, verifyToken, extractBearerToken } from "../lib/auth";
import { inspectIpv6Source, isGlobalUnicastIPv6 } from "../lib/ip";
import { getDevice, upsertDevice } from "../lib/db";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  // Enforce POST method
  if (req.method !== "POST") {
    res.setHeader("Allow", ["POST"]);
    return res.status(405).json({ success: false, error: `Method ${req.method} Not Allowed` });
  }

  try {
    const { deviceId, ipv6, port, token } = req.body || {};

    // 1. Validate deviceId
    if (!validateDeviceId(deviceId)) {
      return res.status(400).json({
        success: false,
        error: "Invalid deviceId: must be 1-64 alphanumeric characters, hyphens or underscores",
      });
    }

    // 2. Validate IPv6
    if (!validateIPv6(ipv6)) {
      return res.status(400).json({
        success: false,
        error: "Invalid ipv6: must be a well-formed IPv6 address string",
      });
    }

    if (!isGlobalUnicastIPv6(ipv6)) {
      return res.status(400).json({
        success: false,
        error: "Invalid ipv6: address must be a globally routable unicast address (not link-local, loopback, or private ULA)",
      });
    }

    // 3. Validate Port
    if (!validatePort(port)) {
      return res.status(400).json({
        success: false,
        error: "Invalid port: must be an integer between 1 and 65535",
      });
    }

    // 4. Authenticate device
    const bearer = extractBearerToken(req);
    const providedToken = bearer || token || (req.headers["x-device-token"] as string);
    const regSecretHeader = (req.headers["x-registration-secret"] as string) || "";
    const expectedRegSecret = process.env.REGISTRATION_SECRET;

    if (!providedToken) {
      return res.status(401).json({
        success: false,
        error: "Missing authorization token in Authorization header or body",
      });
    }

    const existingDevice = await getDevice(deviceId);

    let tokenHashToStore: string;

    if (existingDevice) {
      // Authenticate against stored token hash
      const isAuthValid = verifyToken(deviceId, providedToken, existingDevice.token_hash);
      const isRegSecretValid = expectedRegSecret && (providedToken === expectedRegSecret || regSecretHeader === expectedRegSecret);

      if (!isAuthValid && !isRegSecretValid) {
        return res.status(401).json({
          success: false,
          error: "Unauthorized: invalid device token for this deviceId",
        });
      }

      // If authorized with existing token, keep or update token
      tokenHashToStore = isAuthValid ? existingDevice.token_hash : hashToken(deviceId, providedToken);
    } else {
      // New device registration: verify registration secret if configured
      if (expectedRegSecret) {
        const hasValidRegSecret =
          regSecretHeader === expectedRegSecret ||
          providedToken === expectedRegSecret ||
          (req.headers.authorization && req.headers.authorization.includes(expectedRegSecret));

        if (!hasValidRegSecret) {
          return res.status(403).json({
            success: false,
            error: "Forbidden: valid REGISTRATION_SECRET required to register a new device",
          });
        }
      }
      tokenHashToStore = hashToken(deviceId, providedToken);
    }

    // 5. Inspect edge source IP vs reported IP
    const ipInspection = inspectIpv6Source(req, ipv6);

    // 6. Upsert device record into database
    const saved = await upsertDevice(deviceId, ipv6, port, tokenHashToStore);

    return res.status(200).json({
      success: true,
      deviceId: saved.device_id,
      ipv6: saved.ipv6,
      port: saved.port,
      lastSeen: saved.last_seen,
      sourceIpMatches: ipInspection.sourceIpMatches,
      observedEdgeIp: ipInspection.observedEdgeIp,
      notes: ipInspection.notes,
      message: "Device registered successfully",
    });
  } catch (error: any) {
    console.error("[register] Internal error:", error);
    return res.status(500).json({
      success: false,
      error: "Internal server error",
      details: process.env.NODE_ENV === "development" ? error?.message : undefined,
    });
  }
}

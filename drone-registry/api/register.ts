import type { VercelRequest, VercelResponse } from "@vercel/node";
import { validateDeviceId, validateIPv6, parsePort } from "../lib/validate";
import { hashToken, verifyToken, extractBearerToken } from "../lib/auth";
import { inspectIpv6Source, isGlobalUnicastIPv6 } from "../lib/ip";
import { getDevice, upsertDevice } from "../lib/db";
import { notifyPeerOverUdp } from "../lib/push";

export default async function handler(req: VercelRequest, res: VercelResponse) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type, Authorization, x-device-token, x-registration-secret, x-admin-key");

  if (req.method === "OPTIONS") {
    return res.status(204).end();
  }

  // Enforce POST method
  if (req.method !== "POST") {
    res.setHeader("Allow", ["POST", "OPTIONS"]);
    return res.status(405).json({ success: false, error: `Method ${req.method} Not Allowed` });
  }

  try {
    const rawDeviceId = req.body?.deviceId;
    const deviceId = typeof rawDeviceId === "string" ? rawDeviceId.trim().toUpperCase().replace(/\s+/g, "") : "";
    const { ipv6, token } = req.body || {};
    const rawPort = req.body?.port;

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
    const port = parsePort(rawPort);
    if (port === null) {
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

    // 7. Automatic Peer Notification:
    // If the counterpart peer device is registered, send a direct UDP update push
    const requestedPeerId = typeof req.body?.peerDeviceId === "string" ? req.body.peerDeviceId.trim().toUpperCase() : null;
    const defaultPeerId = deviceId === "DRONE-001" ? "GROUND-001" : deviceId === "GROUND-001" ? "DRONE-001" : null;
    const targetPeerId = requestedPeerId || defaultPeerId;

    let peerTarget: { deviceId: string; ipv6: string; port: number; lastSeen: number } | null = null;
    let peerNotified = false;

    if (targetPeerId) {
      try {
        const peerDevice = await getDevice(targetPeerId);
        if (peerDevice) {
          peerTarget = {
            deviceId: peerDevice.device_id,
            ipv6: peerDevice.ipv6,
            port: peerDevice.port,
            lastSeen: peerDevice.last_seen instanceof Date ? peerDevice.last_seen.getTime() : new Date(peerDevice.last_seen).getTime(),
          };

          // Trigger automatic UDP push of the new IP to the counterpart device
          peerNotified = await notifyPeerOverUdp(peerDevice.ipv6, peerDevice.port, {
            type: "PEER_IP_UPDATE",
            deviceId: saved.device_id,
            ipv6: saved.ipv6,
            port: saved.port,
            timestamp: Date.now(),
          });
        }
      } catch (err: any) {
        console.warn("[register] Peer notification warning:", err?.message);
      }
    }

    return res.status(200).json({
      success: true,
      deviceId: saved.device_id,
      ipv6: saved.ipv6,
      port: saved.port,
      lastSeen: saved.last_seen,
      sourceIpMatches: ipInspection.sourceIpMatches,
      observedEdgeIp: ipInspection.observedEdgeIp,
      notes: ipInspection.notes,
      peerTarget,
      peerNotified,
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

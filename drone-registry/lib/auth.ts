import { createHmac, timingSafeEqual } from "crypto";
import { VercelRequest } from "@vercel/node";

/**
 * Computes an HMAC-SHA256 hash of the device token.
 * We hash device tokens so database leaks don't compromise drone auth tokens.
 */
export function hashToken(deviceId: string, token: string): string {
  const secret = process.env.TOKEN_SECRET || "uav_default_drone_hmac_secret_2026_key";
  return createHmac("sha256", secret)
    .update(`${deviceId}:${token}`)
    .digest("hex");
}

/**
 * Constant-time comparison between stored hex hash and calculated token hash.
 */
export function verifyToken(deviceId: string, rawToken: string, storedHash: string): boolean {
  try {
    const computedHash = hashToken(deviceId, rawToken);
    const bufA = Buffer.from(computedHash, "hex");
    const bufB = Buffer.from(storedHash, "hex");
    if (bufA.length !== bufB.length) return false;
    return timingSafeEqual(bufA, bufB);
  } catch {
    return false;
  }
}

/**
 * Extracts Bearer token from the Authorization header.
 */
export function extractBearerToken(req: VercelRequest): string | null {
  const authHeader = req.headers.authorization;
  if (!authHeader) return null;
  const parts = authHeader.trim().split(/\s+/);
  if (parts.length === 2 && parts[0].toLowerCase() === "bearer") {
    return parts[1];
  }
  return null;
}

/**
 * Verifies if the request provides the administrative secret.
 */
export function verifyAdmin(req: VercelRequest): boolean {
  const adminSecret = process.env.ADMIN_SECRET;
  if (!adminSecret) return false;
  const token = extractBearerToken(req) || (req.headers["x-admin-key"] as string);
  return token === adminSecret;
}

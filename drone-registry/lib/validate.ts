import { isIPv6 } from "net";

/**
 * Device ID must be 1–64 characters: alphanumeric, dash, or underscore.
 * This prevents injection, path traversal, and wildcard abuse.
 */
export function validateDeviceId(id: unknown): id is string {
  if (typeof id !== "string") return false;
  const clean = id.trim().replace(/\s+/g, "");
  if (clean.length < 1 || clean.length > 64) return false;
  return /^[A-Za-z0-9_-]+$/.test(clean);
}

/**
 * Validates a string as a well-formed IPv6 address using Node's built-in
 * net.isIPv6() — reliable, covers compressed forms like ::1 and 2001:db8::1.
 */
export function validateIPv6(ip: unknown): ip is string {
  if (typeof ip !== "string") return false;
  if (ip.length > 45) return false; // max IPv6 string length
  return isIPv6(ip);
}

/**
 * Port must be a non-zero integer in the valid UDP/TCP range (1-65535).
 * Also accepts numeric strings like "5005".
 */
export function validatePort(port: unknown): port is number {
  const p = typeof port === "string" ? Number(port.trim()) : port;
  if (typeof p !== "number" || isNaN(p)) return false;
  return Number.isInteger(p) && p >= 1 && p <= 65535;
}

export function parsePort(port: unknown): number | null {
  const p = typeof port === "string" ? Number(port.trim()) : port;
  if (typeof p === "number" && Number.isInteger(p) && p >= 1 && p <= 65535) {
    return p;
  }
  return null;
}

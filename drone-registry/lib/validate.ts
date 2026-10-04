import { isIPv6 } from "net";

/**
 * Device ID must be 1–64 characters: alphanumeric, dash, or underscore.
 * This prevents injection, path traversal, and wildcard abuse.
 */
export function validateDeviceId(id: unknown): id is string {
  if (typeof id !== "string") return false;
  if (id.length < 1 || id.length > 64) return false;
  return /^[A-Za-z0-9_-]+$/.test(id);
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
 * Port must be a non-zero integer in the valid UDP/TCP range.
 */
export function validatePort(port: unknown): port is number {
  if (typeof port !== "number") return false;
  return Number.isInteger(port) && port >= 1 && port <= 65535;
}

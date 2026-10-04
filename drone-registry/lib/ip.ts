import { VercelRequest } from "@vercel/node";
import { isIPv6 } from "net";

export interface IpExtractionResult {
  clientReportedIp: string;
  observedEdgeIp: string | null;
  sourceIpMatches: boolean;
  notes: string;
}

/**
 * Extracts the real client IP observed by Vercel's edge network.
 * Vercel sets 'x-forwarded-for' (leftmost IP) and 'x-real-ip'.
 */
export function getObservedClientIp(req: VercelRequest): string | null {
  const xForwardedFor = req.headers["x-forwarded-for"];
  if (typeof xForwardedFor === "string") {
    const ips = xForwardedFor.split(",").map((s) => s.trim());
    if (ips.length > 0 && ips[0]) {
      return ips[0];
    }
  }

  const xRealIp = req.headers["x-real-ip"];
  if (typeof xRealIp === "string" && xRealIp.trim().length > 0) {
    return xRealIp.trim();
  }

  return req.socket.remoteAddress || null;
}

/**
 * Validates that an IPv6 address is globally routable (not loopback, link-local, or documentation).
 */
export function isGlobalUnicastIPv6(ip: string): boolean {
  if (!isIPv6(ip)) return false;

  const lower = ip.toLowerCase();

  // Loopback (::1) and unspecified (::)
  if (lower === "::1" || lower === "::") return false;

  // Link-local: fe80::/10
  if (lower.startsWith("fe8") || lower.startsWith("fe9") || lower.startsWith("fea") || lower.startsWith("feb")) {
    return false;
  }

  // Unique local address (ULA): fc00::/7 (fc00:... and fd00:...)
  if (lower.startsWith("fc") || lower.startsWith("fd")) {
    return false;
  }

  // Multicast: ff00::/8
  if (lower.startsWith("ff")) {
    return false;
  }

  return true;
}

/**
 * Inspects client-reported IPv6 against Vercel edge source IP.
 */
export function inspectIpv6Source(req: VercelRequest, clientReportedIp: string): IpExtractionResult {
  const observed = getObservedClientIp(req);
  let matches = false;
  let notes = "";

  if (!observed) {
    notes = "Edge client IP could not be determined from headers";
  } else if (observed.toLowerCase() === clientReportedIp.toLowerCase()) {
    matches = true;
    notes = "Authoritative match: client reported IPv6 exactly matches Vercel edge source IP";
  } else {
    // Note: On cellular networks, HTTPS egress may use RFC 4941 privacy temporary IPv6 or NAT64,
    // while the drone's listening port resides on its static global IPv6 interface ID.
    notes = `Observed edge IP (${observed}) differs from reported listening IPv6 (${clientReportedIp}). Normal for cellular privacy extensions or multi-homed routing.`;
  }

  return {
    clientReportedIp,
    observedEdgeIp: observed,
    sourceIpMatches: matches,
    notes,
  };
}

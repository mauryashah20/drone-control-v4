import dgram from "dgram";

export interface PeerUpdatePayload {
  type: "PEER_IP_UPDATE";
  deviceId: string;
  ipv6: string;
  port: number;
  timestamp: number;
}

/**
 * Sends a lightweight direct UDP notification packet to the peer device's IPv6:port.
 * Packet structure:
 *  - 2 bytes magic header: 0xFF, 0x55
 *  - JSON UTF-8 payload
 * Fails safely and non-blockingly without delaying the HTTP response.
 */
export async function notifyPeerOverUdp(
  peerIpv6: string,
  peerPort: number,
  payload: PeerUpdatePayload,
  timeoutMs: number = 800
): Promise<boolean> {
  return new Promise((resolve) => {
    let socket: dgram.Socket | null = null;
    let timer: NodeJS.Timeout | null = null;

    const cleanup = (success: boolean) => {
      if (timer) clearTimeout(timer);
      if (socket) {
        try {
          socket.close();
        } catch (_) {}
        socket = null;
      }
      resolve(success);
    };

    timer = setTimeout(() => cleanup(false), timeoutMs);

    try {
      const isIpv6 = peerIpv6.includes(":");
      socket = dgram.createSocket(isIpv6 ? "udp6" : "udp4");

      const magic = Buffer.from([0xff, 0x55]);
      const jsonBuf = Buffer.from(JSON.stringify(payload), "utf-8");
      const packet = Buffer.concat([magic, jsonBuf]);

      socket.send(packet, peerPort, peerIpv6, (err) => {
        if (err) {
          console.warn(`[push] UDP send error to [${peerIpv6}]:${peerPort}: ${err.message}`);
          cleanup(false);
        } else {
          console.log(`[push] Sent UDP peer update to [${peerIpv6}]:${peerPort}`);
          cleanup(true);
        }
      });
    } catch (err: any) {
      console.warn(`[push] Socket exception for [${peerIpv6}]:${peerPort}: ${err?.message}`);
      cleanup(false);
    }
  });
}

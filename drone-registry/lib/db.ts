import { sql } from "@vercel/postgres";
import { put, get, del, list } from "@vercel/blob";
import { text } from "node:stream/consumers";

export interface DeviceRecord {
  device_id: string;
  ipv6: string;
  port: number;
  token_hash: string;
  last_seen: Date;
  created_at: Date;
  updated_at: Date;
}

// In-memory fallback store when neither Postgres nor Vercel Blob is configured (or if Blob quota exhausted)
const inMemoryStore = new Map<string, DeviceRecord>();

let blobQuotaExceeded = false;

function markBlobError(err: any): void {
  const msg = (err?.message || "").toLowerCase();
  if (
    msg.includes("quota") ||
    msg.includes("limit") ||
    msg.includes("exceeded") ||
    msg.includes("403") ||
    msg.includes("429") ||
    err?.status === 403 ||
    err?.status === 429
  ) {
    if (!blobQuotaExceeded) {
      console.warn("[db] Vercel Blob request limit exhausted! Automatically switching to high-speed in-memory store.");
    }
    blobQuotaExceeded = true;
  }
}

export function getStorageMode(): "postgres" | "blob" | "memory" {
  if (process.env.USE_MEMORY_DB === "true") return "memory";
  if (process.env.POSTGRES_URL) return "postgres";
  if (!blobQuotaExceeded && process.env.BLOB_READ_WRITE_TOKEN) return "blob";
  return "memory";
}

let dbInitPromise: Promise<void> | null = null;

export async function ensureDb(): Promise<void> {
  if (getStorageMode() !== "postgres") return;
  if (!dbInitPromise) {
    dbInitPromise = initDb().catch((e) => {
      console.error("[db] Auto DB init error:", e);
      dbInitPromise = null;
    });
  }
  await dbInitPromise;
}

export async function initDb(): Promise<void> {
  if (getStorageMode() !== "postgres") return;

  await sql`
    CREATE TABLE IF NOT EXISTS devices (
      device_id   VARCHAR(64)  PRIMARY KEY,
      ipv6        VARCHAR(45)  NOT NULL DEFAULT '',
      port        INTEGER      NOT NULL DEFAULT 0 CHECK (port >= 0 AND port <= 65535),
      token_hash  CHAR(64)     NOT NULL,
      last_seen   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
      created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
      updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
    );
  `;

  await sql`
    CREATE INDEX IF NOT EXISTS idx_devices_last_seen
      ON devices (last_seen DESC);
  `;
}

/**
 * Retrieves a device by its ID.
 */
export async function getDevice(deviceId: string): Promise<DeviceRecord | null> {
  const mode = getStorageMode();

  if (mode === "blob") {
    try {
      const blob = await get(`devices/${deviceId}.json`, { access: "private" });
      if (blob && blob.stream) {
        const str = await text(blob.stream as any);
        const data = JSON.parse(str);
        const record: DeviceRecord = {
          ...data,
          last_seen: new Date(data.last_seen),
          created_at: new Date(data.created_at),
          updated_at: new Date(data.updated_at),
        };
        inMemoryStore.set(deviceId, record);
        return record;
      }
    } catch (err: any) {
      markBlobError(err);
      console.warn("[db] Blob getDevice failed, falling back to memory:", err?.message);
    }
  }

  if (mode === "postgres") {
    try {
      await ensureDb();
      const result = await sql<DeviceRecord>`
        SELECT device_id, ipv6, port, token_hash, last_seen, created_at, updated_at
        FROM devices
        WHERE device_id = ${deviceId}
        LIMIT 1;
      `;
      return result.rows[0] || null;
    } catch (err: any) {
      console.warn("[db] Postgres getDevice failed:", err?.message);
    }
  }

  const rec = inMemoryStore.get(deviceId);
  return rec ? { ...rec } : null;
}

/**
 * Registers a new device or updates an existing device's IPv6 and port.
 */
export async function upsertDevice(
  deviceId: string,
  ipv6: string,
  port: number,
  tokenHash: string
): Promise<DeviceRecord> {
  const now = new Date();
  const mode = getStorageMode();

  if (mode === "blob") {
    try {
      const existing = await getDevice(deviceId);
      const record: DeviceRecord = {
        device_id: deviceId,
        ipv6,
        port,
        token_hash: tokenHash,
        last_seen: now,
        created_at: existing ? existing.created_at : now,
        updated_at: now,
      };
      await put(`devices/${deviceId}.json`, JSON.stringify(record), {
        access: "private",
        allowOverwrite: true,
      });
      inMemoryStore.set(deviceId, record);
      return record;
    } catch (err: any) {
      markBlobError(err);
      console.warn("[db] Blob upsertDevice failed, saving to in-memory store:", err?.message);
    }
  }

  if (mode === "postgres") {
    try {
      await ensureDb();
      const result = await sql<DeviceRecord>`
        INSERT INTO devices (device_id, ipv6, port, token_hash, last_seen, created_at, updated_at)
        VALUES (${deviceId}, ${ipv6}, ${port}, ${tokenHash}, NOW(), NOW(), NOW())
        ON CONFLICT (device_id) DO UPDATE SET
          ipv6 = EXCLUDED.ipv6,
          port = EXCLUDED.port,
          token_hash = EXCLUDED.token_hash,
          last_seen = NOW(),
          updated_at = NOW()
        RETURNING device_id, ipv6, port, token_hash, last_seen, created_at, updated_at;
      `;
      return result.rows[0];
    } catch (err: any) {
      console.warn("[db] Postgres upsertDevice failed:", err?.message);
    }
  }

  const existing = inMemoryStore.get(deviceId);
  const record: DeviceRecord = {
    device_id: deviceId,
    ipv6,
    port,
    token_hash: tokenHash,
    last_seen: now,
    created_at: existing ? existing.created_at : now,
    updated_at: now,
  };
  inMemoryStore.set(deviceId, record);
  return { ...record };
}

/**
 * Lightweight heartbeat: updates last_seen (and optional ipv6 if changed) without changing token.
 */
export async function updateHeartbeat(
  deviceId: string,
  ipv6?: string,
  port?: number
): Promise<DeviceRecord | null> {
  const now = new Date();
  const mode = getStorageMode();

  if (mode === "blob") {
    try {
      const existing = await getDevice(deviceId);
      if (existing) {
        existing.last_seen = now;
        existing.updated_at = now;
        if (ipv6 !== undefined) existing.ipv6 = ipv6;
        if (port !== undefined) existing.port = port;

        await put(`devices/${deviceId}.json`, JSON.stringify(existing), {
          access: "private",
          allowOverwrite: true,
        });
        inMemoryStore.set(deviceId, existing);
        return existing;
      }
    } catch (err: any) {
      markBlobError(err);
      console.warn("[db] Blob updateHeartbeat failed, updating in-memory store:", err?.message);
    }
  }

  if (mode === "postgres") {
    try {
      if (ipv6 !== undefined && port !== undefined) {
        const result = await sql<DeviceRecord>`
          UPDATE devices
          SET last_seen = NOW(),
              updated_at = NOW(),
              ipv6 = ${ipv6},
              port = ${port}
          WHERE device_id = ${deviceId}
          RETURNING device_id, ipv6, port, token_hash, last_seen, created_at, updated_at;
        `;
        return result.rows[0] || null;
      } else if (ipv6 !== undefined) {
        const result = await sql<DeviceRecord>`
          UPDATE devices
          SET last_seen = NOW(),
              updated_at = NOW(),
              ipv6 = ${ipv6}
          WHERE device_id = ${deviceId}
          RETURNING device_id, ipv6, port, token_hash, last_seen, created_at, updated_at;
        `;
        return result.rows[0] || null;
      } else if (port !== undefined) {
        const result = await sql<DeviceRecord>`
          UPDATE devices
          SET last_seen = NOW(),
              updated_at = NOW(),
              port = ${port}
          WHERE device_id = ${deviceId}
          RETURNING device_id, ipv6, port, token_hash, last_seen, created_at, updated_at;
        `;
        return result.rows[0] || null;
      } else {
        const result = await sql<DeviceRecord>`
          UPDATE devices
          SET last_seen = NOW(),
              updated_at = NOW()
          WHERE device_id = ${deviceId}
          RETURNING device_id, ipv6, port, token_hash, last_seen, created_at, updated_at;
        `;
        return result.rows[0] || null;
      }
    } catch (err: any) {
      console.warn("[db] Postgres updateHeartbeat failed:", err?.message);
    }
  }

  const existing = inMemoryStore.get(deviceId);
  if (!existing) return null;

  existing.last_seen = now;
  existing.updated_at = now;
  if (ipv6 !== undefined) existing.ipv6 = ipv6;
  if (port !== undefined) existing.port = port;

  inMemoryStore.set(deviceId, existing);
  return { ...existing };
}

/**
 * Deletes a device registration.
 */
export async function deleteDevice(deviceId: string): Promise<boolean> {
  const mode = getStorageMode();

  if (mode === "blob") {
    try {
      await del(`devices/${deviceId}.json`);
      return true;
    } catch {
      return false;
    }
  }

  if (mode === "postgres") {
    try {
      const result = await sql`
        DELETE FROM devices
        WHERE device_id = ${deviceId};
      `;
      return (result.rowCount ?? 0) > 0;
    } catch (err: any) {
      console.warn("[db] Postgres deleteDevice failed:", err?.message);
    }
  }

  return inMemoryStore.delete(deviceId);
}

/**
 * Lists all devices (for monitoring/admin).
 */
export async function listAllDevices(): Promise<Omit<DeviceRecord, "token_hash">[]> {
  const mode = getStorageMode();

  if (mode === "blob") {
    try {
      const { blobs } = await list({ prefix: "devices/" });
      const results = await Promise.allSettled(
        blobs.map(async (b) => {
          try {
            const blobData = await get(b.url, { access: "private" });
            if (!blobData || !blobData.stream) return null;
            const str = await text(blobData.stream as any);
            const { token_hash, ...rest } = JSON.parse(str);
            return {
              ...rest,
              last_seen: new Date(rest.last_seen),
              created_at: new Date(rest.created_at),
              updated_at: new Date(rest.updated_at),
            } as Omit<DeviceRecord, "token_hash">;
          } catch {
            return null;
          }
        })
      );

      const records: Omit<DeviceRecord, "token_hash">[] = [];
      for (const res of results) {
        if (res.status === "fulfilled" && res.value) {
          records.push(res.value);
        }
      }
      return records.sort((a, b) => b.last_seen.getTime() - a.last_seen.getTime());
    } catch (err: any) {
      markBlobError(err);
      console.warn("[db] Blob listAllDevices failed, falling back to memory:", err?.message);
    }
  }

  if (mode === "postgres") {
    try {
      const result = await sql<Omit<DeviceRecord, "token_hash">>`
        SELECT device_id, ipv6, port, last_seen, created_at, updated_at
        FROM devices
        ORDER BY last_seen DESC;
      `;
      return result.rows.map((row) => ({
        ...row,
        last_seen: new Date(row.last_seen),
        created_at: new Date(row.created_at),
        updated_at: new Date(row.updated_at),
      }));
    } catch (err: any) {
      console.warn("[db] Postgres listAllDevices failed:", err?.message);
    }
  }

  return Array.from(inMemoryStore.values())
    .map(({ token_hash, ...rest }) => ({ ...rest }))
    .sort((a, b) => b.last_seen.getTime() - a.last_seen.getTime());
}

/**
 * Clear in-memory store (for test resets).
 */
export function clearMemoryDb(): void {
  inMemoryStore.clear();
}

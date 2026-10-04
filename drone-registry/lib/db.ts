import { sql } from "@vercel/postgres";

export interface DeviceRecord {
  device_id: string;
  ipv6: string;
  port: number;
  token_hash: string;
  last_seen: Date;
  created_at: Date;
  updated_at: Date;
}

// In-memory fallback store when POSTGRES_URL is not set (e.g. local offline test/dev)
const inMemoryStore = new Map<string, DeviceRecord>();

function isMemoryMode(): boolean {
  return !process.env.POSTGRES_URL || process.env.USE_MEMORY_DB === "true";
}

let dbInitPromise: Promise<void> | null = null;

export async function ensureDb(): Promise<void> {
  if (isMemoryMode()) return;
  if (!dbInitPromise) {
    dbInitPromise = initDb().catch((e) => {
      console.error("[db] Auto DB init error:", e);
      dbInitPromise = null;
    });
  }
  await dbInitPromise;
}

/**
 * Initializes the database schema if running against PostgreSQL.
 */
export async function initDb(): Promise<void> {
  if (isMemoryMode()) {
    return;
  }

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
  if (isMemoryMode()) {
    const rec = inMemoryStore.get(deviceId);
    return rec ? { ...rec } : null;
  }

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
    console.warn("[db] Postgres getDevice failed, falling back to memory:", err?.message);
    const rec = inMemoryStore.get(deviceId);
    return rec ? { ...rec } : null;
  }
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

  if (isMemoryMode()) {
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
    console.warn("[db] Postgres upsertDevice failed, storing in memory:", err?.message);
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

  if (isMemoryMode()) {
    const existing = inMemoryStore.get(deviceId);
    if (!existing) return null;

    existing.last_seen = now;
    existing.updated_at = now;
    if (ipv6 !== undefined) existing.ipv6 = ipv6;
    if (port !== undefined) existing.port = port;

    inMemoryStore.set(deviceId, existing);
    return { ...existing };
  }

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
}

/**
 * Deletes a device registration.
 */
export async function deleteDevice(deviceId: string): Promise<boolean> {
  if (isMemoryMode()) {
    return inMemoryStore.delete(deviceId);
  }

  const result = await sql`
    DELETE FROM devices
    WHERE device_id = ${deviceId};
  `;
  return (result.rowCount ?? 0) > 0;
}

/**
 * Lists all devices (for monitoring/admin).
 */
export async function listAllDevices(): Promise<Omit<DeviceRecord, "token_hash">[]> {
  if (isMemoryMode()) {
    return Array.from(inMemoryStore.values())
      .map(({ token_hash, ...rest }) => ({ ...rest }))
      .sort((a, b) => b.last_seen.getTime() - a.last_seen.getTime());
  }

  const result = await sql<Omit<DeviceRecord, "token_hash">>`
    SELECT device_id, ipv6, port, last_seen, created_at, updated_at
    FROM devices
    ORDER BY last_seen DESC;
  `;
  return result.rows;
}

/**
 * Clear in-memory store (for test resets).
 */
export function clearMemoryDb(): void {
  inMemoryStore.clear();
}

-- ============================================================
-- Drone IPv6 Registry — Database Schema
-- Compatible with: Vercel Postgres (Neon), Supabase, any Postgres
-- Run once to initialise.
-- ============================================================

CREATE TABLE IF NOT EXISTS devices (
    -- Primary identifier for the drone (e.g. "DRONE-001")
    -- Validated server-side: alphanumeric, dash, underscore only
    device_id   VARCHAR(64)  PRIMARY KEY,

    -- Current globally-routable IPv6 address on the LTE interface
    -- NULL until the drone sends its first registration heartbeat
    ipv6        VARCHAR(45)  NOT NULL DEFAULT '',

    -- UDP/TCP port the drone is listening on (e.g. 14550 for MAVLink)
    port        INTEGER      NOT NULL DEFAULT 0
                             CHECK (port >= 0 AND port <= 65535),

    -- HMAC-SHA256(TOKEN_SECRET, "deviceId:rawToken") — hex-encoded, 64 chars
    -- The raw token is NEVER stored.
    token_hash  CHAR(64)     NOT NULL,

    -- Timestamp of the most recent register or heartbeat call
    last_seen   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Immutable — when this device was first added to the registry
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Mutable — updated on every register/heartbeat
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Index for fast online/offline queries sorted by recency
CREATE INDEX IF NOT EXISTS idx_devices_last_seen
    ON devices (last_seen DESC);

-- ============================================================
-- Optional: automatic updated_at trigger
-- ============================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER devices_updated_at
    BEFORE UPDATE ON devices
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

# Drone IPv6 Discovery & Registration System

A lightweight, serverless device registration and discovery service designed for UAVs operating on cellular LTE connections with dynamic global IPv6 prefixes.

> **CRITICAL ARCHITECTURAL GUARANTEE:**
> - Vercel is **STRICTLY** a registration and discovery lookup server.
> - Vercel **NEVER** proxies, tunnels, or relays MAVLink telemetry, video streams, or control packets.
> - All drone ↔ ground station communications remain **100% DIRECT peer-to-peer IPv6 connections**.
> - Zero VPNs (no ZeroTier, no Tailscale), no NAT traversal, no relay servers in the data plane.

---

## Architecture Diagram

```
                 ┌─────────────────────────────────┐
                 │          Vercel Cloud           │
                 │      Registration API           │
                 │  https://drone-reg.vercel.app   │
                 └───────────────┬─────────────────┘
                                 │
           1. HTTPS Heartbeat    │   2. HTTPS Lookup
           (every 15s or on IP   │   (find current IPv6)
              prefix change)     │
                                 │
       ┌─────────────────────────┴─────────────────────────┐
       ▼                                                   ▼
┌──────────────┐                                    ┌──────────────┐
│   Drone Pi   │                                    │Ground Station│
│   (LTE IPv6) │◄────────── DIRECT IPv6 ───────────►│ (Laptop/GCS) │
└──────────────┘      MAVLink UDP 14550 / Video     └──────────────┘
                       (Zero Relay Detour)
```

---

## Repository Structure

```
drone video + telemetry/
├── drone-registry/                  # Vercel Serverless Discovery API (TypeScript)
│   ├── api/
│   │   ├── register.ts              # POST /api/register (full registration)
│   │   ├── lookup.ts                # GET  /api/lookup?deviceId=... (ground station discovery)
│   │   ├── heartbeat.ts             # POST /api/heartbeat (lightweight keep-alive)
│   │   ├── devices.ts               # GET/DELETE /api/devices (admin & unregistration)
│   │   └── health.ts                # GET  /api/health (service health probe)
│   ├── lib/
│   │   ├── auth.ts                  # HMAC-SHA256 token hashing & constant-time verify
│   │   ├── db.ts                    # PostgreSQL database layer (@vercel/postgres) with offline fallback
│   │   ├── ip.ts                    # Vercel edge IP extraction & global unicast validator
│   │   └── validate.ts              # Input sanitization (device ID, IPv6, port)
│   ├── scripts/
│   │   └── gen_device.ts            # CLI tool to provision device tokens & SQL inserts
│   ├── tests/
│   │   └── test_all.py              # Automated 10-point test suite
│   ├── schema.sql                   # Database schema for PostgreSQL / Neon
│   ├── vercel.json                  # Vercel function configuration & security headers
│   ├── package.json                 # Node dependencies
│   └── tsconfig.json                # TypeScript compiler configuration
├── drone-client/                    # Raspberry Pi Client (Python)
│   ├── detect_ipv6.py               # Cellular modem IPv6 address parser (wwan0, etc.)
│   ├── drone_daemon.py              # Background daemon: auto-register on boot & prefix change
│   ├── drone_registry.service       # Systemd unit file for autostart on Pi boot
│   └── requirements.txt             # Python requests dependency
└── ground-client/                   # Ground Station Client (Python)
    ├── lookup.py                    # Resolves drone IPv6 via Vercel discovery API
    ├── direct_ipv6_test.py          # End-to-end direct IPv6 TCP/UDP/Ping test tool
    └── requirements.txt             # Python requests dependency
```

---

## Environment Variables

Configure these in your Vercel Dashboard (`Settings -> Environment Variables`) or in `.env`:

| Variable | Required | Description |
|---|---|---|
| `POSTGRES_URL` | **Yes** (Prod) | Vercel Postgres / Neon connection string (`postgres://...sslmode=require`). Automatically provided when adding Vercel Postgres storage. |
| `TOKEN_SECRET` | **Yes** | 32-byte secret hex key used to HMAC-hash device tokens before storing. Generate with `openssl rand -hex 32`. |
| `REGISTRATION_SECRET` | Optional | Shared secret required when provisioning a brand-new device without pre-seeding the DB. |
| `ADMIN_SECRET` | Optional | Admin key for `GET /api/devices` and deleting devices. |
| `ONLINE_THRESHOLD_SECONDS` | Optional | Seconds after last heartbeat before device is marked offline (Default: `60`). |

---

## Database Setup

1. In your [Vercel Dashboard](https://vercel.com/dashboard), navigate to **Storage** -> **Create Database** -> **Postgres** (Neon).
2. Connect the database to your `drone-registry` project. This automatically populates `POSTGRES_URL`.
3. In the Vercel Postgres Query tab (or your SQL client), run `schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS devices (
    device_id   VARCHAR(64)  PRIMARY KEY,
    ipv6        VARCHAR(45)  NOT NULL DEFAULT '',
    port        INTEGER      NOT NULL DEFAULT 0 CHECK (port >= 0 AND port <= 65535),
    token_hash  CHAR(64)     NOT NULL,
    last_seen   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_devices_last_seen
    ON devices (last_seen DESC);
```

---

## API Endpoints

### 1. `POST /api/register`
Full registration called on drone startup or whenever the LTE interface acquires a new IPv6 address.

- **Headers:**
  - `Content-Type: application/json`
  - `Authorization: Bearer <DEVICE_TOKEN>`
- **Request Body:**
  ```json
  {
    "deviceId": "DRONE-001",
    "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
    "port": 14550
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "success": true,
    "deviceId": "DRONE-001",
    "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
    "port": 14550,
    "lastSeen": "2026-10-03T13:00:00.000Z",
    "sourceIpMatches": true,
    "observedEdgeIp": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
    "message": "Device registered successfully"
  }
  ```

### 2. `GET /api/lookup?deviceId=:id`
Queried by Ground Station to discover the drone's current direct IPv6 address.

- **Response (200 OK):**
  ```json
  {
    "success": true,
    "deviceId": "DRONE-001",
    "ipv6": "2401:4900:1c1c:beef:cafe:0001:0002:0003",
    "port": 14550,
    "lastSeen": "2026-10-03T13:00:10.000Z",
    "isOnline": true,
    "secondsSinceLastSeen": 5,
    "onlineThresholdSeconds": 60
  }
  ```

### 3. `POST /api/heartbeat`
Lightweight periodic keep-alive from the drone (typically every 15s).

- **Headers:** `Authorization: Bearer <DEVICE_TOKEN>`
- **Request Body:**
  ```json
  {
    "deviceId": "DRONE-001"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "success": true,
    "deviceId": "DRONE-001",
    "lastSeen": "2026-10-03T13:00:25.000Z",
    "isOnline": true
  }
  ```

---

## Deployment to Vercel

```bash
cd "drone-registry"

# 1. Login to Vercel
npx vercel login

# 2. Link & Deploy to Preview
npx vercel

# 3. Add Environment Variables in Dashboard:
#    - POSTGRES_URL (linked from Vercel Postgres)
#    - TOKEN_SECRET = $(openssl rand -hex 32)
#    - REGISTRATION_SECRET = $(openssl rand -hex 16)

# 4. Deploy to Production
npx vercel --prod
```

---

## Example cURL Commands

### Provision Device Credentials
```bash
# Generate device token and hash
npx ts-node scripts/gen_device.ts DRONE-001
```

### Register Drone
```bash
curl -X POST https://your-drone-registry.vercel.app/api/register \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_DEVICE_TOKEN" \
  -d '{
    "deviceId": "DRONE-001",
    "ipv6": "2401:4900:1c1c:dead:beef:cafe:0001:1001",
    "port": 14550
  }'
```

### Lookup Drone from Ground Station
```bash
curl "https://your-drone-registry.vercel.app/api/lookup?deviceId=DRONE-001"
```

### Heartbeat Pulse
```bash
curl -X POST https://your-drone-registry.vercel.app/api/heartbeat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_DEVICE_TOKEN" \
  -d '{"deviceId": "DRONE-001"}'
```

---

## Drone Pi Setup & Autostart

1. Copy `drone-client/` to the Raspberry Pi:
   ```bash
   scp -r drone-client pi@raspberrypi.local:/home/pi/drone-client
   ```
2. Install Python dependencies on Pi:
   ```bash
   pip3 install -r /home/pi/drone-client/requirements.txt
   ```
3. Configure `/etc/drone-registry.env`:
   ```bash
   REGISTRY_URL="https://your-drone-registry.vercel.app"
   DEVICE_ID="DRONE-001"
   DEVICE_TOKEN="YOUR_DEVICE_TOKEN"
   PORT=14550
   POLL_INTERVAL=15
   ```
4. Enable and start the systemd daemon:
   ```bash
   sudo cp /home/pi/drone-client/drone_registry.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable drone_registry
   sudo systemctl start drone_registry
   sudo systemctl status drone_registry
   ```

---

## Ground Station Usage

### 1. Simple Discovery
```bash
python ground-client/lookup.py --url https://your-drone-registry.vercel.app --id DRONE-001
```

### 2. Verify Direct IPv6 Connectivity
```bash
python ground-client/direct_ipv6_test.py 2401:4900:1c1c:dead:beef:cafe:0001:1001 --port 14550
```

---

## Security & Vercel Limitations

### 1. Token Hashing
Raw tokens are **never** stored in the database. Tokens are hashed with HMAC-SHA256 using a server-side `TOKEN_SECRET`. Constant-time comparison (`timingSafeEqual`) prevents timing side-channel attacks.

### 2. Carrier NAT & Privacy Extension IPv6
On cellular networks (e.g., Jio, Airtel), outgoing HTTPS connections from the Pi may use RFC 4941 temporary privacy addresses or NAT64, while incoming MAVLink/UDP packets must reach the modem interface identifier. The server inspects the edge source IP header (`x-forwarded-for` / `x-real-ip`) and flags whether the edge IP exactly matches the listening IP without discarding the listening interface address.

### 3. Vercel Serverless Characteristics
- **Stateless Execution:** Serverless functions do not keep persistent memory across invocations. All persistence relies on PostgreSQL.
- **Cold Starts:** Neon connection pooling enables quick responses (<150ms cold, ~15ms warm).
- **HTTP/HTTPS Only:** Vercel functions cannot handle incoming UDP packets — ensuring by physical architecture that Vercel can never become an accidental UDP relay for drone video/telemetry.

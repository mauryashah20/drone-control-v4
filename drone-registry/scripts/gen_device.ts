import { randomBytes, createHmac } from "crypto";

/**
 * CLI utility to generate a secure device token and print the configuration
 * for both the Drone Raspberry Pi and the database.
 *
 * Usage:
 *   npx ts-node scripts/gen_device.ts <deviceId> [tokenSecret]
 */
function main() {
  const args = process.argv.slice(2);
  const deviceId = args[0] || `DRONE-${randomBytes(3).toString("hex").toUpperCase()}`;
  const tokenSecret = args[1] || process.env.TOKEN_SECRET || "CHANGE_ME_SECRET_KEY_FOR_LOCAL_DEV";
  const rawToken = randomBytes(24).toString("hex");

  const tokenHash = createHmac("sha256", tokenSecret)
    .update(`${deviceId}:${rawToken}`)
    .digest("hex");

  console.log("=================================================");
  console.log("  Drone IPv6 Registry — Device Credential Generator");
  console.log("=================================================\n");
  console.log(`Device ID:      ${deviceId}`);
  console.log(`Raw Token:      ${rawToken}`);
  console.log(`Token Hash:     ${tokenHash}`);
  console.log(`Token Secret:   ${tokenSecret}\n`);

  console.log("--- Raspberry Pi Configuration (.env on drone) ---");
  console.log(`REGISTRY_URL="https://your-drone-registry.vercel.app"`);
  console.log(`DEVICE_ID="${deviceId}"`);
  console.log(`DEVICE_TOKEN="${rawToken}"`);
  console.log(`DEFAULT_PORT="14550"\n`);

  console.log("--- SQL Pre-provisioning (Optional) ---");
  console.log(
    `INSERT INTO devices (device_id, ipv6, port, token_hash, last_seen, created_at, updated_at)`
  );
  console.log(
    `VALUES ('${deviceId}', '', 14550, '${tokenHash}', NOW(), NOW(), NOW())`
  );
  console.log(`ON CONFLICT (device_id) DO UPDATE SET token_hash = EXCLUDED.token_hash;\n`);

  console.log("--- Example Registration Curl ---");
  console.log(`curl -X POST https://your-drone-registry.vercel.app/api/register \\`);
  console.log(`  -H "Content-Type: application/json" \\`);
  console.log(`  -H "Authorization: Bearer ${rawToken}" \\`);
  console.log(`  -d '{"deviceId":"${deviceId}","ipv6":"2401:4900:...","port":14550}'\n`);
  console.log("=================================================");
}

main();

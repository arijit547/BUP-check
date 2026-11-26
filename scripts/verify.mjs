import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { benchmark } from "./benchmark.mjs";
import { evaluatePolicy } from "./evaluate-policy.mjs";
import { json } from "./simulator.mjs";

const mode = process.argv[2];
if (!["e2e", "benchmark", "release", "forecast-policy", "reorder-policy"].includes(mode)) {
  throw new Error("Usage: node scripts/verify.mjs e2e|benchmark|release|forecast-policy|reorder-policy");
}

const root = fileURLToPath(new URL("../", import.meta.url));
const output = resolve(root, "artifacts", mode);
mkdirSync(output, { recursive: true });
const project = `bup-m5-${mode}-${randomUUID().slice(0, 8)}`;
const compose = ["compose", "-p", project, "-f", "compose.yaml"];
if (mode === "release") compose.push("-f", "compose.release.yaml");

// Reserve all three ports together to keep the test stack separate from the operator stack.
const listeners = await Promise.all(Array.from({ length: 3 }, () => new Promise((resolvePort, reject) => {
  const server = createServer();
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => resolvePort(server));
})));
const [simulator, api, dashboard] = listeners.map(server => server.address().port);
await Promise.all(listeners.map(server => new Promise(done => server.close(done))));
const base = port => `http://127.0.0.1:${port}`;
const env = {
  ...process.env,
  SIMULATOR_START_MODE: "paused",
  TICK_MINUTES: "15",
  SIMULATOR_PORT: String(simulator),
  SNAPSHOT_PORT: String(api),
  DASHBOARD_PORT: String(dashboard),
  BUP_TEST_SIMULATOR: base(simulator),
  BUP_TEST_DASHBOARD: base(dashboard),
  BUP_TEST_OUTPUT: output,
};

function run(command, args, { capture = false, required = true } = {}) {
  const result = spawnSync(command, args, {
    cwd: root, env, encoding: "utf8", stdio: capture ? "pipe" : "inherit", timeout: 300_000,
  });
  if (required && (result.error || result.status !== 0)) {
    throw new Error(`${command} ${args.join(" ")} failed: ${result.error ?? result.stderr ?? result.status}`);
  }
  return capture ? (result.stdout ?? "") + (result.stderr ?? "") : result.status;
}

try {
  run("docker", [...compose, "config", "--quiet"]);
  const startup = mode === "release"
    ? ["--no-build", "--pull", env.BUP_RELEASE_PULL ?? "always"]
    : ["--build"];
  run("docker", [...compose, "up", ...startup, "--wait", "--wait-timeout", "120"]);
  run("docker", [...compose, "exec", "-T", "snapshot-api", "python",
    "-X", "pycache_prefix=/tmp/bup-verify-cache", "-m", "compileall", "-q", "/app"]);
  run("docker", [...compose, "exec", "-T", "snapshot-api", "python",
    "-X", "pycache_prefix=/tmp/bup-verify-cache", "-m", "unittest", "discover", "-s", "/app/tests", "-v"]);
  const [health, contract, snapshot, ledger] = await Promise.all([
    json(`${base(simulator)}/v1/health`),
    json(`${base(api)}/openapi.json`),
    json(`${base(dashboard)}/api/snapshot`),
    json(`${base(dashboard)}/api/approvals`),
  ]);
  assert.deepEqual(contract, JSON.parse(readFileSync(resolve(root, "docs/openapi.json"), "utf8")));
  assert.equal(health.status, "ok");
  assert.equal(snapshot.simulation.tick, 0);
  assert.equal(snapshot.simulation.status, "PAUSED");
  assert.ok(snapshot.depots.length && snapshot.stations.length && snapshot.routes.length);
  assert.deepEqual(ledger.attempts, []);
  assert.equal(ledger.simulator_error, null);
  assert.equal(ledger.rebind_required, false);
  const healthEvidence = {
    project, mode, simulator: health,
    snapshot: {
      tick: snapshot.simulation.tick, depots: snapshot.depots.length,
      stations: snapshot.stations.length, routes: snapshot.routes.length,
    },
    contract_matches: true, backend_compiles: true, backend_tests_passed: true, approval_ledger_empty: true,
  };
  writeFileSync(resolve(output, "health.json"), JSON.stringify(healthEvidence, null, 2) + "\n");
  console.log(`Health, backend compilation and saved OpenAPI checks passed for ${project}`);
  if (mode === "e2e") {
    run(process.execPath, ["frontend/node_modules/@playwright/test/cli.js", "test", "--config", "frontend/playwright.config.mjs"]);
  }
  if (mode === "benchmark") {
    const containers = {};
    for (const service of ["simulator-api", "snapshot-api", "operator-dashboard"]) {
      containers[service] = run("docker", [...compose, "ps", "-q", service], { capture: true }).trim();
    }
    await benchmark(env, containers);
  }
  if (mode === "forecast-policy" || mode === "reorder-policy") {
    await evaluatePolicy(mode, env);
  }
} finally {
  const services = run("docker", [...compose, "ps"], { capture: true, required: false });
  const logs = run("docker", [...compose, "logs", "--no-color", "--tail", "100"], { capture: true, required: false });
  writeFileSync(resolve(output, "compose-status.txt"), services);
  writeFileSync(resolve(output, "compose.log"), logs);
  const down = run("docker", [...compose, "down", "--volumes"], { required: false });
  if (down !== 0) process.exitCode = 1;
}

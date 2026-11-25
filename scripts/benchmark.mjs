import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { promisify } from "node:util";
import { json, seedHistory } from "./simulator.mjs";

const exec = promisify(execFile);

function positiveInteger(value, fallback, max) {
  const result = Number(value ?? fallback);
  if (!Number.isInteger(result) || result < 1 || result > max) {
    throw new Error(`Expected a benchmark parameter between 1 and ${max}, got ${value}`);
  }
  return result;
}

export async function benchmark(env, containers) {
  const concurrency = positiveInteger(env.BUP_BENCH_CONCURRENCY, 2, 64);
  const durationSeconds = positiveInteger(env.BUP_BENCH_SECONDS, 15, 180);
  const simulator = env.BUP_TEST_SIMULATOR;
  const endpoint = "/api/planning?history_ticks=8&horizon_ticks=96";
  const url = env.BUP_TEST_DASHBOARD + endpoint;
  await seedHistory(simulator);
  const startState = await json(`${simulator}/v1/instance`);
  for (let i = 0; i < 3; i++) {
    const report = await json(url);
    assert.equal(report.tick, startState.tick);
    assert.ok(report.recommendations.length);
  }

  const samples = [];
  const latency = [];
  const allLatency = [];
  const errors = {};
  async function usageSample() {
    const { stdout } = await exec("docker", [
      "stats", "--no-stream", "--format", "{{json .}}", ...Object.values(containers),
    ], { timeout: 10_000 });
    const rows = stdout.trim().split(/\r?\n/).map(line => JSON.parse(line));
    return Object.fromEntries(rows.map(row => [
      Object.keys(containers).find(service => containers[service].startsWith(row.ID)),
      { cpu: row.CPUPerc, memory: row.MemUsage },
    ]));
  }

  const started = performance.now();
  const deadline = started + durationSeconds * 1000;
  async function sampleResources() {
    while (performance.now() < deadline) {
      const services = await usageSample();
      samples.push({ elapsedSeconds: Number(((performance.now() - started) / 1000).toFixed(3)), services });
      const remaining = deadline - performance.now();
      if (remaining > 0) await sleep(Math.min(1500, remaining));
    }
  }

  async function worker() {
    while (performance.now() < deadline) {
      const begin = performance.now();
      let failure;
      try {
        const response = await fetch(url, { signal: AbortSignal.timeout(5000) });
        if (!response.ok) {
          await response.text();
          failure = `HTTP ${response.status}`;
        } else {
          const body = await response.json();
          if (body.tick !== startState.tick || !body.recommendations?.length) failure = "Unexpected report";
        }
      } catch (error) {
        failure = error.name;
      }
      const elapsed = performance.now() - begin;
      allLatency.push(elapsed);
      if (failure) errors[failure] = (errors[failure] ?? 0) + 1;
      else latency.push(elapsed);
    }
  }

  const resourceTask = sampleResources();
  const loadTask = Promise.all(Array.from({ length: concurrency }, worker))
    .then(() => (performance.now() - started) / 1000);
  const [elapsedSeconds] = await Promise.all([loadTask, resourceTask]);
  const postflight = await Promise.allSettled([
    json(`${simulator}/v1/instance`, { signal: AbortSignal.timeout(5000) }),
    json(`${simulator}/v1/allocations`, { signal: AbortSignal.timeout(5000) }),
  ]);
  const [end, allocations] = postflight;
  assert.ok(samples.length && samples.every(sample => Object.keys(sample.services).length === Object.keys(containers).length));
  function percentiles(values) {
    if (!values.length) return null;
    const sorted = values.toSorted((a, b) => a - b);
    const percentile = p => Number(sorted[Math.ceil(p * sorted.length) - 1].toFixed(3));
    return { p50: percentile(0.5), p95: percentile(0.95), p99: percentile(0.99) };
  }
  const errorCount = Object.values(errors).reduce((total, value) => total + value, 0);
  const requestCount = latency.length + errorCount;
  const report = {
    collected_at: new Date().toISOString(), endpoint, simulator_tick: startState.tick,
    simulator_seed: startState.seed, concurrency, warmup_requests: 3,
    target_duration_seconds: durationSeconds, elapsed_seconds: Number(elapsedSeconds.toFixed(3)),
    requests: requestCount, successful_requests: latency.length,
    throughput_requests_per_second: Number((requestCount / elapsedSeconds).toFixed(3)),
    successful_requests_per_second: Number((latency.length / elapsedSeconds).toFixed(3)),
    success_latency_ms: percentiles(latency), all_latency_ms: percentiles(allLatency),
    errors: errorCount, error_rate_percent: Number((100 * errorCount / requestCount).toFixed(3)),
    errors_by_type: errors, resource_samples: samples,
    postflight: {
      tick: end.status === "fulfilled" ? end.value.tick : null,
      status: end.status === "fulfilled" ? end.value.status : null,
      allocations: allocations.status === "fulfilled" ? allocations.value.length : null,
      probe_errors: postflight.flatMap(result => result.status === "rejected"
        ? [result.reason.message] : []),
    },
  };
  await writeFile(resolve(env.BUP_TEST_OUTPUT, `planning-benchmark-${concurrency}.json`), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report, null, 2));
  assert.ok(requestCount, "Benchmark collected no requests");
  if (end.status === "fulfilled") {
    assert.equal(end.value.tick, startState.tick);
    assert.equal(end.value.status, "PAUSED");
  }
  if (allocations.status === "fulfilled") assert.deepEqual(allocations.value, []);
}

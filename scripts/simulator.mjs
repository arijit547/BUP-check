export async function json(url, init) {
  const response = await fetch(url, { signal: AbortSignal.timeout(10_000), ...init });
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${url}: HTTP ${response.status}`);
  return response.json();
}

export function admin(base, path, body) {
  return json(`${base}/admin/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export async function seedHistory(base) {
  const start = await json(`${base}/v1/instance`);
  if (start.status !== "PAUSED" || start.tick !== 0) {
    throw new Error("Expected a fresh paused simulator at tick 0");
  }
  for (let tick = 0; tick < 42; tick++) {
    const result = await admin(base, "step");
    if (result.tick !== tick + 1) throw new Error("Unexpected simulator tick after stepping");
  }
}

export async function fetchJsonWithMeta<T>(
  url: string,
  options?: RequestInit,
): Promise<{ data: T; headers: Headers }> {
  const response = await fetch(url, { ...options, cache: "no-store" });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Retain the HTTP status when the server does not return JSON.
    }
    throw new Error(message);
  }
  return { data: (await response.json()) as T, headers: response.headers };
}

export async function fetchJson<T>(url: string, options?: RequestInit): Promise<T> {
  const { data } = await fetchJsonWithMeta<T>(url, options);
  return data;
}

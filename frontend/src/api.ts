function assertSafeApiUrl(url: string): string {
  if (!url.startsWith("/api/") || url.includes("://") || url.startsWith("//")) {
    throw new Error(`Disallowed API request path: ${url}`);
  }
  const resolved = new URL(url, window.location.origin);
  if (resolved.origin !== window.location.origin || !resolved.pathname.startsWith("/api/")) {
    throw new Error(`Disallowed API request origin: ${resolved.origin}`);
  }
  return `${resolved.pathname}${resolved.search}`;
}

export async function fetchJsonWithMeta<T>(
  url: string,
  options?: RequestInit,
): Promise<{ data: T; headers: Headers }> {
  const safeUrl = assertSafeApiUrl(url);
  const response = await fetch(safeUrl, { ...options, cache: "no-store" });
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

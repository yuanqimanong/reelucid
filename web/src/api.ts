export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new Error(body.error?.message || `请求失败 (${response.status})`);
  }
  return response.status === 204
    ? (undefined as T)
    : (response.json() as Promise<T>);
}
export const json = (value: unknown, method = "POST"): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

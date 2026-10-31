export type Json = Record<string, any>;
export class ApiError extends Error {
  constructor(
    message: string,
    public status = 0,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
function detailText(value: any): string {
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) return detailText(parsed);
    } catch {
      /* Plain message. */
    }
    return value;
  }
  if (Array.isArray(value))
    return value.map((v) => v.message || v.msg || String(v)).join(" ");
  return "The server could not complete this request.";
}
export async function api<T = any>(
  path: string,
  body?: unknown,
  method?: string,
  timeout = 150000,
): Promise<T> {
  const token = sessionStorage.getItem("sg_access_token");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    let response: Response;
    try {
      response = await fetch("/api" + path, {
        method: method || (body === undefined ? "GET" : "POST"),
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: "Bearer " + token } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    } catch (e) {
      throw new ApiError(
        (e as Error).name === "AbortError"
          ? "The server is taking longer than expected. Reconnect and check the run before trying again."
          : "The local server is offline. Open Start Session Generator.command in the project folder, then reconnect. Your choices are saved.",
      );
    }
    const text = await response.text();
    let result: any;
    try {
      result = text ? JSON.parse(text) : {};
    } catch {
      throw new ApiError(
        "The server returned an unreadable response. Restart Session Generator and reconnect.",
        response.status,
      );
    }
    if (!response.ok)
      throw new ApiError(
        response.status === 401
          ? "This server requires an access token. Enter it below to reconnect."
          : detailText(result.detail || result),
        response.status,
      );
    return result;
  } finally {
    clearTimeout(timer);
  }
}
export const pretty = (v: unknown) => JSON.stringify(v, null, 2);
export const number = (n: number | undefined) =>
  n === undefined ? "—" : new Intl.NumberFormat().format(n);
export const time = (n: number | null) =>
  n
    ? new Date(n * 1000).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "Not started";

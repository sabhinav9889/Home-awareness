// Same-origin JSON API client: adds the bearer token and turns errors into ApiError.
import * as auth from "./auth.js";

export const SESSION_ENDED = "Your session ended — please log in again.";

export class ApiError extends Error {
  constructor(status, detail, body = null) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.body = body; // the parsed error body (e.g. a 409's {"by": ...})
  }
}

// FastAPI details are a string, or a list of validation errors for 422.
function detailText(body, status) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) return d.map((e) => e.msg).join("; ");
  return `Request failed (${status})`;
}

export async function request(method, path, body) {
  const token = auth.token();
  const headers = { Accept: "application/json" };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "Can't reach the server — check your connection.");
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (res.ok) return data;
  // A 401 on a call that carried a session means the session is gone (expired, reset,
  // removed). Without one (e.g. a wrong password at login) it is just the server's answer.
  // Only if it is still the current session: a late answer to an old one must not end a new.
  if (res.status === 401 && token && token === auth.token()) auth.logout(SESSION_ENDED);
  const err = new ApiError(res.status, detailText(data, res.status), data);
  const retry = Number.parseInt(res.headers.get("Retry-After") || "", 10);
  if (Number.isFinite(retry) && retry >= 0) err.retryAfter = retry; // seconds, on a 429
  throw err;
}

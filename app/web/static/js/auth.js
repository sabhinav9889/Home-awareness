// Session: the JWT lives in sessionStorage only (gone when the tab closes), never logged.
import { request } from "./api.js";
import { navigate } from "./router.js";

const KEY = "ha.token";
let flash = null;

export function token() {
  try {
    return sessionStorage.getItem(KEY);
  } catch {
    return null;
  }
}

export async function login(email, password) {
  const { access_token } = await request("POST", "/auth/login", { email, password });
  try {
    sessionStorage.setItem(KEY, access_token);
    flash = null;
  } catch {
    throw new Error("This browser blocks session storage, so you can't stay logged in. " +
      "Allow site data for this page (or leave private mode) and try again.");
  }
}

export function logout(msg) {
  sessionStorage.removeItem(KEY);
  flash = msg || null;
  navigate("#/login");
}

// The message the login view shows after a logout. It stays while the person is on the
// login view, however often it re-renders (several 401s can each trigger one), and goes on
// a successful login or once another public view is shown (app.js calls clearFlash()).
export function peekFlash() {
  return flash;
}

export function clearFlash() {
  flash = null;
}

// A new household (householdName + timezone), or joining one with an invite token.
export function signup({ email, password, householdName, timezone, inviteToken }) {
  if (inviteToken) {
    return request("POST", "/auth/signup", { email, password, invite_token: inviteToken });
  }
  return request("POST", "/auth/signup", {
    email, password, household_name: householdName, timezone,
  });
}

export function confirm(tokenValue) {
  return request("POST", "/auth/confirm", { token: tokenValue });
}

export function resend(email) {
  return request("POST", "/auth/confirm/resend", { email });
}

export function requestReset(email) {
  return request("POST", "/auth/password-reset/request", { email });
}

export function resetPassword(tokenValue, newPassword) {
  return request("POST", "/auth/password-reset/confirm", {
    token: tokenValue, new_password: newPassword,
  });
}

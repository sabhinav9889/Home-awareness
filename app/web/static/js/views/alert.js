// The alert screen: when an alert arrives over the shared socket it takes over the page,
// on every logged-in view. "I'm on it" acknowledges it for the whole household.
//
// Alerts queue: the newest shows, with "+N more"; acknowledging, dismissing or closing one
// reveals the next. Alerts are tracked by event (test alerts, which have no event, by alert):
// an alert_ack for a tracked event from someone else turns its screen into "X is handling
// it"; my own ack (from another tab or device) just closes it. Acks for anything else are
// ignored. Server strings only ever become text nodes.
//
// Photos: an alert with a camera snapshot fetches it with the session token as a blob and
// shows it from an object URL (the token is never in a URL). The URL is revoked when the alert
// leaves the queue or the session resets. No snapshot, or a 404: "No photo".
//
// Catch-up: alerts sent while the socket was down are lost to it, so on every transition to
// "live" (first connect included) the screen asks GET /alerts and shows the ones from the
// last CATCH_UP_MS nobody has acknowledged and this tab hasn't seen, with the member's current
// popup/alarm preferences. Older alerts are never resurrected.
//
// Stray keys (P3-R4): someone typing or half-asleep must not clear an alarm screen. When the
// screen appears or its shown alert changes, focus goes to the dialog itself (not a button),
// every button stays disabled for GUARD_MS, and Escape does nothing: acknowledging or
// dismissing takes a deliberate click, or Tab to the button and Enter.
import { request, requestBlob } from "../api.js";
import { subscribe } from "../live.js";
import { notifyIfHidden, startBeep } from "../notify.js";
import { navigate } from "../router.js";
import { el } from "../ui.js";

const KEEP_QUEUED = 50; // bound for a tab left open while many alerts pile up
const KEEP_SEEN = 500;
const UNREACHABLE = "Couldn't reach the server — try again";
const GUARD_MS = 600;
const CATCH_UP_MS = 10 * 60_000;

let queue = []; // oldest first; the screen shows the last one
const seen = new Set(); // alert ids already handled here (a duplicate message is ignored)
let me = null; // Promise<member id | null>
let timeZone;
let screen = null;
let returnFocus = null;
let shown = ""; // "key:state" on screen, to move focus only when that changes
let guardUntil = 0; // performance.now() before which the buttons stay disabled
let guardTimer = null;
let stopBeep = null;
let beepKey = null;
let subscribed = false;
let generation = 0; // bumped by reset(): a catch-up that started before a logout is dropped

function validTimeZone(tz) {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: tz });
    return tz;
  } catch {
    return undefined; // the browser's own
  }
}

// After a session exists (app.js, on every private route). Idempotent.
export function start({ timezone } = {}) {
  if (timezone) timeZone = validTimeZone(timezone);
  if (!me) {
    me = request("GET", "/auth/me").then((m) => m.id).catch(() => {
      me = null; // retried on the next route
      return null;
    });
  }
  if (!subscribed) {
    subscribed = true;
    // household.js fires this after a PATCH: use a new timezone at once, even on screen.
    window.addEventListener("household-updated", (ev) => {
      const tz = ev.detail && ev.detail.timezone;
      if (!tz || !me) return; // logged out meanwhile
      timeZone = validTimeZone(tz);
      render();
    });
    subscribe({ onMessage, onStatus: (status) => { if (status === "live") catchUp(); } });
  }
}

// On logout (any public view): forget everything, close the screen.
export function reset() {
  generation += 1;
  for (const item of queue) dispose(item);
  queue = [];
  seen.clear();
  me = null;
  timeZone = undefined;
  silence();
  render();
}

function onMessage(msg) {
  if (msg.kind === "alert") onAlert(msg);
  else if (msg.kind === "alert_ack") onAck(msg);
}

const keyOf = (eventId, alertId) => (eventId == null ? `a${alertId}` : `e${eventId}`);

async function catchUp() {
  const mine = generation;
  let alerts;
  let prefs;
  try {
    [alerts, prefs] = await Promise.all([
      request("GET", "/alerts"), request("GET", "/me/alert-preferences"),
    ]);
  } catch {
    return; // offline again or logged out: the next "live" tries again
  }
  if (mine !== generation || !Array.isArray(alerts)) return;
  const since = Date.now() - CATCH_UP_MS;
  const missed = alerts.filter((a) => a.acknowledged_by == null && !seen.has(a.id)
    && Date.parse(a.created_at) >= since).reverse(); // oldest first: the newest ends on top
  let added = null;
  for (const a of missed) {
    added = onAlert({
      alert_id: a.id, event_id: a.event_id, label: a.label, device_name: a.device_name,
      room_name: a.room_name, at: a.created_at, popup: prefs.popup || prefs.alarm, alarm: false, // one beep, below
    }) || added;
  }
  if (added && prefs.alarm) {
    silence();
    stopBeep = startBeep(); // only while the tab is visible
    beepKey = added.key;
  }
}

// Returns the new queue item, or null if the message added nothing.
function onAlert(msg) {
  if (!msg.popup && !msg.alarm) return null; // an alarm needs a screen to stop it from
  if (seen.has(msg.alert_id)) return null;
  seen.add(msg.alert_id);
  if (seen.size > KEEP_SEEN) seen.delete(seen.values().next().value);
  const key = keyOf(msg.event_id, msg.alert_id);
  if (queue.some((i) => i.key === key)) return null;
  const item = {
    key,
    alertId: msg.alert_id,
    eventId: msg.event_id,
    test: msg.event_id == null,
    label: msg.label || "Alert",
    device: msg.device_name,
    room: msg.room_name || null,
    at: msg.at,
    state: "open", // "open" | "handled"
    by: null,
    already: false, // learned from our own 409 rather than a live ack
    busy: false,
    error: null,
    photo: msg.event_id == null ? "none" : msg.has_snapshot === false ? "missing" : "loading",
    photoUrl: null,
    photoImg: null,
    gone: false,
  };
  queue.push(item);
  while (queue.length > KEEP_QUEUED) dispose(queue.shift());
  if (item.photo === "loading") loadPhoto(item);
  item.note = notifyIfHidden(item.label,
    item.test ? "Test alert from Home Awareness" : `${item.device || "A sensor"}${item.room ? ` in ${item.room}` : ""} · ${time(item.at)}`,
    key);
  if (msg.alarm) {
    silence();
    stopBeep = startBeep();
    beepKey = key;
  }
  render();
  return item;
}

async function onAck(msg) {
  if (msg.event_id == null) return; // a test alert's ack: only the acking tab knows which
  const key = `e${msg.event_id}`;
  if (!queue.some((i) => i.key === key)) return;
  const myId = await (me || Promise.resolve(null));
  const item = queue.find((i) => i.key === key); // still there after the wait?
  if (!item) return;
  if (beepKey === key) silence();
  if (myId != null && msg.by_member_id === myId) {
    remove(item);
    return;
  }
  item.state = "handled";
  item.by = msg.by_email || "Someone";
  item.already = false;
  item.error = null;
  render();
}

function silence() {
  if (stopBeep) stopBeep();
  stopBeep = null;
  beepKey = null;
}

function closeNote(item) {
  try {
    if (item.note) item.note.close();
  } catch { /* gone */ }
}

// An alert leaves the queue for good: close its notification, free its photo.
function dispose(item) {
  closeNote(item);
  item.gone = true;
  if (item.photoUrl) URL.revokeObjectURL(item.photoUrl);
  item.photoUrl = null;
  item.photoImg = null;
}

async function loadPhoto(item) {
  let blob = null;
  try {
    blob = await requestBlob(`/snapshots/${encodeURIComponent(item.eventId)}`);
  } catch { /* offline or an error: no photo to show */ }
  if (item.gone) return; // closed while loading: nothing to revoke yet
  if (blob) {
    item.photoUrl = URL.createObjectURL(blob);
    item.photo = "ready";
  } else {
    item.photo = "missing";
  }
  if (queue.at(-1) === item) render();
}

function remove(item) {
  dispose(item);
  queue = queue.filter((i) => i !== item);
  render();
}

async function acknowledge(item) {
  silence();
  item.busy = true;
  item.error = null;
  render();
  try {
    await request("POST", `/alerts/${item.alertId}/ack`);
    remove(item);
    return;
  } catch (err) {
    item.busy = false;
    if (err.status === 409) {
      item.state = "handled";
      item.by = (err.body && err.body.by) || "Someone";
      item.already = true;
    } else if (err.status === 0) {
      item.error = UNREACHABLE;
    } else if (err.status === 401) {
      return; // logged out already
    } else {
      item.error = err.detail || UNREACHABLE;
    }
  }
  if (queue.includes(item)) render();
}

function time(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    timeZone, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  }).format(d);
}

function icon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({
    width: 46, height: 46, viewBox: "0 0 24 24", fill: "none", stroke: "#FFFFFF",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true",
  })) svg.setAttribute(k, v);
  for (const d of ["M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    "M12 9v4", "M12 17h.01"]) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return el("div", { class: "alert-ring", "aria-hidden": "true" },
    el("div", { class: "alert-core" }, svg));
}

function button(label, action, onclick, primary = false, disabled = false) {
  return el("button", {
    type: "button", class: primary ? "alert-btn alert-btn-primary" : "alert-btn",
    "data-action": action, disabled: disabled || performance.now() < guardUntil, onclick,
  }, label);
}

function photoBlock(item) {
  if (item.photo === "none") return null;
  if (item.photo === "ready") {
    if (!item.photoImg) {
      item.photoImg = el("img", { class: "alert-photo", src: item.photoUrl,
        alt: `Camera snapshot: ${item.label}` });
    }
    return item.photoImg;
  }
  return el("p", { class: "alert-nophoto" },
    item.photo === "loading" ? "Loading photo…" : "No photo");
}

function content(item, more) {
  const at = time(item.at);
  const handled = item.state === "handled";
  const actions = handled
    ? el("div", { class: "alert-actions" },
      button("Close", "close", () => remove(item), true))
    : el("div", { class: "alert-actions" },
      button(item.busy ? "Sending…" : "I'm on it", "ack", () => acknowledge(item), true,
        item.busy),
      el("div", { class: "alert-actions-row" },
        button("Event details", "details", () => {
          silence();
          remove(item);
          navigate("#/overview");
        }),
        button("Dismiss", "dismiss", () => {
          silence();
          remove(item);
        })));
  return el("div", { class: "alert-inner" },
    el("div", { class: "alert-top" },
      el("span", { class: "dot", "aria-hidden": "true" }),
      el("span", { class: "alert-brand" }, "Home Awareness"),
      more ? el("span", { class: "alert-more" }, `+${more} more`) : null),
    el("div", { class: "alert-main" },
      icon(),
      el("h1", { id: "alert-title" }, item.label),
      item.test ? null : el("div", { id: "alert-where", class: "alert-device" },
        item.room ? [el("strong", { class: "alert-room" }, item.room),
          ` · via ${item.device || "a sensor"}`] : (item.device || "A sensor")),
      photoBlock(item),
      el("div", { id: "alert-desc", class: "alert-sub" },
        item.test ? `Your alerts reach this dashboard${at ? ` · ${at}` : ""}` : at),
      handled ? el("p", { class: "alert-handled", role: "status" },
        `${item.by} is ${item.already ? "already " : ""}handling it`) : null,
      item.error ? el("p", { class: "alert-error", role: "alert" }, item.error) : null),
    actions);
}

function setInert(on) {
  const root = document.getElementById("root");
  if (root) root.inert = on;
}

function render() {
  const item = queue.at(-1);
  if (!item) {
    if (screen) {
      screen.remove();
      screen = null;
      shown = "";
      guardUntil = 0;
      clearTimeout(guardTimer);
      setInert(false);
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      returnFocus = null;
    }
    return;
  }
  if (!screen) {
    returnFocus = document.activeElement;
    screen = el("div", {
      class: "alert-screen", role: "alertdialog", "aria-modal": "true",
      "aria-live": "assertive", "aria-labelledby": "alert-title", tabindex: "-1",
    });
    document.body.append(screen);
    setInert(true);
  }
  const now = `${item.key}:${item.state}`;
  const changed = now !== shown;
  shown = now;
  if (changed) {
    guardUntil = performance.now() + GUARD_MS;
    clearTimeout(guardTimer);
    guardTimer = setTimeout(render, GUARD_MS + 10); // re-enable the buttons
  }
  const active = document.activeElement;
  const focused = active !== screen && screen.contains(active) ? active.dataset.action : null;
  screen.replaceChildren(content(item, queue.length - 1));
  // Only ids that exist: a test alert has no "where" line.
  screen.setAttribute("aria-describedby",
    ["alert-where", "alert-desc"].filter((id) => screen.querySelector(`#${id}`)).join(" "));
  if (changed) {
    screen.focus(); // never a button: a stray Enter or Space must not act
    return;
  }
  const target = focused && screen.querySelector(`[data-action="${focused}"]`);
  if (target && !target.disabled) target.focus();
  else if (!screen.contains(document.activeElement)) screen.focus();
}

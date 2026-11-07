// connect(): one socket with reconnect/backoff. startLive/stopLive/subscribe (below): the
// app-wide connection every view shares.
// The live socket (/ws): parsed messages to onMessage, "live"/"reconnecting" to onStatus.
// Reconnects with backoff 1, 2, 4 … 30 s. The backoff only resets once a socket has stayed
// open for `stableMs`, so a server that accepts and then drops at once isn't hammered.
// 4401/4403 mean the session is gone: log out. The URL carries the token (the server requires
// it there), so it is never logged.
import { SESSION_ENDED } from "./api.js";
import * as auth from "./auth.js";

const SESSION_GONE = new Set([4401, 4403]);

export function connect({
  onMessage, onStatus = () => {}, baseDelayMs = 1000, maxDelayMs = 30_000, stableMs = 10_000,
}) {
  let ws = null;
  let retryTimer = null;
  let stableTimer = null;
  let attempt = 0;
  let stopped = false;

  function open() {
    retryTimer = null;
    const t = auth.token();
    if (stopped || !t) return;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const sock = new WebSocket(`${scheme}//${location.host}/ws?token=${encodeURIComponent(t)}`);
    ws = sock;
    sock.onopen = () => {
      stableTimer = setTimeout(() => { attempt = 0; }, stableMs);
      onStatus("live");
    };
    sock.onmessage = (ev) => {
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg && typeof msg === "object") onMessage(msg);
    };
    sock.onclose = (ev) => {
      clearTimeout(stableTimer);
      if (ws !== sock || stopped) return;
      ws = null;
      if (SESSION_GONE.has(ev.code)) {
        stopped = true;
        auth.logout(SESSION_ENDED);
        return;
      }
      onStatus("reconnecting");
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** attempt);
      attempt += 1;
      retryTimer = setTimeout(open, delay);
    };
  }

  open();
  return function close() {
    stopped = true;
    clearTimeout(retryTimer);
    clearTimeout(stableTimer);
    if (ws) {
      const sock = ws;
      ws = null;
      sock.close(1000);
    }
  };
}

// --- the app's one shared connection ---
// app.js starts it once a session exists and stops it on any public view (logout). Views
// and the alert screen subscribe; a subscriber gets the current status at once if the
// socket is already open, so a view mounted later still sees "live" and later changes.
// <html data-live="live|reconnecting|connecting|off"> mirrors the status (tests wait on it).
const subscribers = new Set();
let closeShared = null;
let sharedStatus = "off";

function setShared(status) {
  sharedStatus = status;
  document.documentElement.dataset.live = status;
  if (status !== "live" && status !== "reconnecting") return; // subscribers see connect()'s two
  for (const s of [...subscribers]) s.onStatus?.(status);
}

export function startLive() {
  if (closeShared) return;
  setShared("connecting");
  closeShared = connect({
    onMessage: (msg) => {
      for (const s of [...subscribers]) {
        try {
          s.onMessage?.(msg);
        } catch (err) {
          console.error("live subscriber failed", err); // one view's bug can't starve others
        }
      }
    },
    onStatus: setShared,
  });
}

export function stopLive() {
  if (!closeShared) return;
  const close = closeShared;
  closeShared = null;
  close();
  setShared("off");
}

// subscribe({onMessage, onStatus}) -> unsubscribe()
export function subscribe(sub) {
  subscribers.add(sub);
  if (sharedStatus === "live" || sharedStatus === "reconnecting") sub.onStatus?.(sharedStatus);
  return () => subscribers.delete(sub);
}

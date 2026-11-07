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

// The live socket (/ws): parsed messages to onMessage, "live"/"reconnecting" to onStatus.
// Reconnects with backoff 1, 2, 4 … 30 s. 4401/4403 mean the session is gone: log out.
// The URL carries the token (the server requires it there), so it is never logged.
import { SESSION_ENDED } from "./api.js";
import * as auth from "./auth.js";

const SESSION_GONE = new Set([4401, 4403]);
const MAX_DELAY_S = 30;

export function connect({ onMessage, onStatus = () => {} }) {
  let ws = null;
  let timer = null;
  let attempt = 0;
  let stopped = false;

  function open() {
    timer = null;
    const t = auth.token();
    if (stopped || !t) return;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    const sock = new WebSocket(`${scheme}//${location.host}/ws?token=${encodeURIComponent(t)}`);
    ws = sock;
    sock.onopen = () => {
      attempt = 0;
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
      if (ws !== sock || stopped) return;
      ws = null;
      if (SESSION_GONE.has(ev.code)) {
        stopped = true;
        auth.logout(SESSION_ENDED);
        return;
      }
      onStatus("reconnecting");
      const delay = Math.min(MAX_DELAY_S, 2 ** attempt);
      attempt += 1;
      timer = setTimeout(open, delay * 1000);
    };
  }

  open();
  return function close() {
    stopped = true;
    clearTimeout(timer);
    if (ws) {
      const sock = ws;
      ws = null;
      sock.close(1000);
    }
  };
}

// Getting attention for an alert: a browser notification (hidden tab only) and a beep
// (visible tab only). Permission is never requested here: that needs a person's click
// (the "Alert channels" button). Failures are swallowed: an alert must never break on them.

const BEEP_MAX_MS = 30_000;
const TONES = [880, 660]; // Hz, alternating
const TONE_MS = 400;

// A notification if the tab is hidden and permission is already "granted"; else null.
// Clicking it brings the tab forward. The caller closes it once the alert is handled.
export function notifyIfHidden(title, body, tag) {
  try {
    if (document.visibilityState !== "hidden") return null;
    if (!("Notification" in window) || Notification.permission !== "granted") return null;
    const note = new Notification(title, { body, tag });
    note.onclick = () => {
      window.focus();
      note.close();
    };
    return note;
  } catch {
    return null; // e.g. mobile Chrome only allows notifications from a service worker
  }
}

// A two-tone beep while the page is visible. Returns stop() (idempotent). It stops by
// itself after 30 s or when the page is hidden. Browsers may keep audio suspended until
// the person has interacted with the page; that is not an error worth showing.
export function startBeep({ maxMs = BEEP_MAX_MS } = {}) {
  if (document.visibilityState !== "visible") return () => {};
  let ctx = null;
  let osc = null;
  let tick = null;
  let limit = null;
  let stopped = false;

  function onVisibility() {
    if (document.visibilityState !== "visible") stop();
  }

  function stop() {
    if (stopped) return;
    stopped = true;
    clearInterval(tick);
    clearTimeout(limit);
    document.removeEventListener("visibilitychange", onVisibility);
    try {
      if (osc) osc.stop();
    } catch { /* never started */ }
    try {
      if (ctx) Promise.resolve(ctx.close()).catch(() => {});
    } catch { /* already closed */ }
  }

  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return () => {};
    ctx = new Ctx();
    const gain = ctx.createGain();
    gain.gain.value = 0.15;
    gain.connect(ctx.destination);
    osc = ctx.createOscillator();
    osc.type = "sine";
    osc.frequency.setValueAtTime(TONES[0], ctx.currentTime);
    osc.connect(gain);
    osc.start();
    let n = 0;
    tick = setInterval(() => {
      n += 1;
      osc.frequency.setValueAtTime(TONES[n % TONES.length], ctx.currentTime);
    }, TONE_MS);
    if (ctx.resume) Promise.resolve(ctx.resume()).catch(() => {});
  } catch {
    stop();
    return () => {};
  }
  limit = setTimeout(stop, maxMs);
  document.addEventListener("visibilitychange", onVisibility);
  return stop;
}

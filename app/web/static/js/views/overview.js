// Overview: household status, sensors, live events (over /ws) and the alert-channel summary.
// It subscribes to the app's shared socket (live.js) before the first fetch, so nothing
// published in between is missed; leaving the view unsubscribes (the socket stays open).
// Events are keyed by id, so the initial fetch, live messages and the catch-up fetch after
// every reconnect never produce duplicate rows.
import { request, requestBlob } from "../api.js";
import { subscribe } from "../live.js";
import { el } from "../ui.js";

const FETCH_LIMIT = 200; // enough history for today's counts
const CATCH_UP_LIMIT = 50; // after a reconnect: what may have been missed
const TABLE_ROWS = 50;
const KEEP_EVENTS = 500; // memory bounds for a tab left open for days
const KEEP_ALERTS = 500;
const TICK_MS = 10_000; // "heartbeat 12 s ago" refresh
const DEVICES_REFRESH_MS = 30_000; // fresh last_seen_at; first heartbeats publish nothing
const FIRST_CONNECT_WAIT_MS = 3000; // load the data once the socket listens (or gave up)
const ALERTS_REFRESH_MS = 800; // after a live event: did it alert me?
const EARLY_MAX = 500; // messages held while loading; past this, catch up instead
const NOTICE_MS = 8000; // how long "X turned Away on" stays

// Shown when the sensor's heartbeat says its microphone input level is clipping.
export const MIC_CLIPPING_NOTE = "Microphone is overloading — lower its input level";

export const KINDS = { laptop: "Laptop", phone: "Phone", pi: "Raspberry Pi", other: "Sensor" };

function validTimeZone(tz) {
  try {
    new Intl.DateTimeFormat(undefined, { timeZone: tz });
    return tz;
  } catch {
    return "UTC";
  }
}

// Time formatting in the household's timezone, not the browser's.
function clock(timeZone) {
  const day = new Intl.DateTimeFormat("en-CA", {
    timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  });
  const time = new Intl.DateTimeFormat(undefined, {
    timeZone, hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  const hm = new Intl.DateTimeFormat(undefined, {
    timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const date = new Intl.DateTimeFormat(undefined, { timeZone, day: "numeric", month: "short" });
  const dayKey = (d) => day.format(d);
  const isToday = (d) => dayKey(d) === dayKey(new Date());
  return {
    isToday,
    time: (d) => (isToday(d) ? time.format(d) : `${date.format(d)}, ${time.format(d)}`),
    short: (d) => (isToday(d) ? hm.format(d) : `${date.format(d)}, ${hm.format(d)}`),
  };
}

export function ago(iso) {
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

function plural(n, one, many = `${one}s`) {
  return `${n} ${n === 1 ? one : many}`;
}

export async function overviewView(_params, { household }) {
  const fmt = clock(validTimeZone(household.timezone));
  const events = new Map(); // event id -> {id, label, device_name, confidence, received_at, merged}
  const alerts = new Map(); // alert id -> {event_id, label, device_name, at, test}
  let devices = new Map(); // device id -> /devices row
  let truncated = false; // older events exist that we don't hold
  let closed = false;
  const guard = { away: Boolean(household.away), start: household.night_start,
    end: household.night_end };
  const photos = new Map(); // event id -> {state: "loading"|"ready"|"missing", url}
  const openPhotos = new Set(); // event ids whose photo row is expanded

  function addEvents(rows) {
    for (const e of rows) if (!events.has(e.id)) events.set(e.id, e);
    if (events.size > KEEP_EVENTS) {
      for (const e of sorted().slice(KEEP_EVENTS)) events.delete(e.id);
      truncated = true;
    }
  }

  function addAlerts(rows) {
    for (const a of rows) {
      alerts.set(a.id, {
        event_id: a.event_id, label: a.label, device_name: a.device_name, at: a.created_at,
        test: a.sound_key === "test",
      });
    }
    if (alerts.size > KEEP_ALERTS) {
      const oldest = [...alerts].sort(([, a], [, b]) => (a.at > b.at) - (a.at < b.at));
      for (const [id] of oldest.slice(0, alerts.size - KEEP_ALERTS)) alerts.delete(id);
    }
  }

  function setDevices(rows) {
    devices = new Map(rows.map((d) => [d.id, d]));
  }

  const sorted = () => [...events.values()].sort((a, b) =>
    (b.received_at > a.received_at) - (b.received_at < a.received_at) || b.id - a.id);

  // --- socket first: messages wait in `early` until the data has loaded ---
  const pill = el("span", { class: "live-pill", role: "status" });
  let status = "connecting";
  let loaded = false;
  const early = [];
  let earlyOverflow = false;
  let liveGen = 0; // counts transitions to "live"
  let firstStatus;
  const firstStatusSeen = new Promise((resolve) => { firstStatus = resolve; });
  setStatus(status);
  const unsubscribe = subscribe({
    onMessage: (msg) => {
      if (loaded) onMessage(msg);
      else if (early.length < EARLY_MAX) early.push(msg);
      else earlyOverflow = true;
    },
    onStatus: (s) => {
      firstStatus();
      if (closed) return;
      const wasLive = status === "live";
      if (s === "live" && !wasLive) liveGen += 1;
      status = s;
      setStatus(s);
      // Back after a gap (or first opened only after the data loaded): fetch what was missed.
      if (s === "live" && !wasLive && loaded) catchUp();
    },
  });

  let prefs;
  let liveAtFetch;
  try {
    await Promise.race([firstStatusSeen,
      new Promise((resolve) => setTimeout(resolve, FIRST_CONNECT_WAIT_MS))]);
    liveAtFetch = status === "live" ? liveGen : -1; // which live stretch the fetch covers
    const [ev, al, dv, pr] = await Promise.all([
      request("GET", `/events?limit=${FETCH_LIMIT}`),
      request("GET", `/alerts?limit=${FETCH_LIMIT}`),
      request("GET", "/devices"),
      request("GET", "/me/alert-preferences"),
    ]);
    addEvents(ev);
    truncated = truncated || ev.length === FETCH_LIMIT;
    addAlerts(al);
    setDevices(dv);
    prefs = pr;
  } catch (err) {
    closed = true;
    unsubscribe();
    throw err;
  }

  // --- DOM ---
  const statusBox = el("section", { class: "status-card", "aria-label": "Status" });
  const sensorGrid = el("div", { class: "sensor-grid" });
  const tbody = el("tbody");
  const emptyNote = el("p", { class: "empty-note" },
    "No events yet. Sensors report sounds here as they hear them.");
  const feed = el("section", { class: "panel events", "aria-labelledby": "feed-h",
    "aria-busy": "false" },
  el("h2", { id: "feed-h" }, "Live events"),
  el("div", { class: "table-scroll" },
    el("table", { class: "events-table", "aria-labelledby": "feed-h" },
      el("thead", {}, el("tr", {},
        ["Time", "Sound", "Sensor", "Confidence", "Result"].map((h) =>
          el("th", { scope: "col", class: h === "Confidence" ? "col-conf" : null }, h)))),
      tbody)),
  emptyNote);

  function setStatus(kind) {
    pill.className = `live-pill is-${kind}`;
    pill.replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }),
      { live: "Live", reconnecting: "Reconnecting…", connecting: "Connecting…" }[kind]);
  }

  function renderStatus() {
    const all = [...devices.values()];
    const offline = all.filter((d) => d.status === "offline").length;
    const online = all.filter((d) => d.status === "online").length;
    const real = [...alerts.values()].filter((a) => !a.test);
    const today = real.filter((a) => fmt.isToday(new Date(a.at)));
    const evToday = [...events.values()].filter((e) => fmt.isToday(new Date(e.received_at)));
    const oldest = sorted().at(-1);
    const more = truncated && oldest && fmt.isToday(new Date(oldest.received_at)) ? "+" : "";
    const last = real.sort((a, b) => (b.at > a.at) - (b.at < a.at))[0];
    const head = offline ? `${plural(offline, "sensor")} offline`
      : all.length ? "All quiet right now" : "No sensors yet";
    const parts = [all.length ? `${online} of ${plural(all.length, "sensor")} listening`
      : "Add a sensor to start listening"];
    if (last) {
      parts.push(`last alert ${fmt.short(new Date(last.at))}, ${last.label}` +
        (last.device_name ? ` in ${last.device_name}` : ""));
    }
    statusBox.className = `status-card${offline ? " has-offline" : ""}`;
    statusBox.replaceChildren(
      el("div", { class: "status-icon", "aria-hidden": "true" }, offline ? "!" : "✓"),
      el("div", { class: "status-text" },
        el("div", { class: "status-head" }, head),
        el("div", { class: "status-sub" }, parts.join(" · "))),
      el("div", { class: "status-counts" },
        el("div", {}, el("div", { class: "count count-events" }, `${evToday.length}${more}`),
          "events today"),
        el("div", {}, el("div", { class: "count count-alerts" }, String(today.length)),
          "alerts today")));
  }

  function sensorCard(d) {
    const kind = Object.hasOwn(KINDS, d.kind) ? KINDS[d.kind] : "Sensor";
    let state, sub;
    if (d.status === "online") {
      state = "Listening";
      sub = d.last_seen_at ? `${kind} · heartbeat ${ago(d.last_seen_at)}` : kind;
    } else if (d.status === "offline") {
      state = d.last_seen_at ? `Offline · last heartbeat ${ago(d.last_seen_at)}` : "Offline";
      sub = `${kind} · alerts from it have stopped`;
    } else {
      state = "Waiting for its first heartbeat";
      sub = kind;
    }
    return el("article", { class: `sensor-card sensor-${d.status}` },
      el("div", { class: "sensor-state" }, el("span", { class: "dot", "aria-hidden": "true" }),
        el("span", {}, state)),
      el("div", { class: "sensor-name" }, d.name),
      el("div", { class: "sensor-sub" }, sub),
      d.mic_clipping ? el("div", { class: "sensor-note" }, MIC_CLIPPING_NOTE) : null);
  }

  function renderSensors() {
    const list = [...devices.values()].sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
    sensorGrid.replaceChildren(...(list.length ? list.map(sensorCard)
      : [el("p", { class: "empty-note" }, "No sensors paired yet.")]));
  }

  // "Alerted": I got an alert for it. "Merged": folded into another sensor's report.
  // "Logged": stored, nothing sent. (The API doesn't say when cooldown held an alert
  // back, so the mockup's "Cooldown" chip isn't shown; those events read "Logged".)
  function result(e, alerted) {
    if (alerted.has(e.id)) return ["Alerted", "chip-alerted"];
    if (e.merged) return ["Merged", "chip-merged"];
    return ["Logged", "chip-logged"];
  }

  function renderEvents() {
    const alerted = new Set([...alerts.values()].map((a) => a.event_id));
    const rows = sorted().slice(0, TABLE_ROWS);
    tbody.replaceChildren(...rows.map((e) => {
      const [text, cls] = result(e, alerted);
      const row = el("tr", {},
        el("td", { class: "cell-time" }, fmt.time(new Date(e.received_at))),
        el("td", { class: "cell-sound" }, e.label),
        el("td", {}, e.device_name || "Removed sensor"),
        el("td", { class: "col-conf" }, Number(e.confidence).toFixed(2)),
        el("td", {}, el("span", { class: `chip ${cls}` }, text),
          e.has_snapshot ? photoButton(e) : null));
      return openPhotos.has(e.id) ? [row, photoRow(e)] : [row];
    }).flat());
    emptyNote.hidden = rows.length > 0;
  }

  function photoButton(e) {
    const open = openPhotos.has(e.id);
    const btn = el("button", { type: "button", class: "link-btn photo-btn",
      "aria-expanded": String(open) }, "Photo");
    btn.addEventListener("click", () => {
      if (openPhotos.has(e.id)) openPhotos.delete(e.id);
      else {
        openPhotos.add(e.id);
        loadPhoto(e.id);
      }
      renderEvents();
    });
    return btn;
  }

  function photoRow(e) {
    const ph = photos.get(e.id);
    const body = ph && ph.state === "ready"
      ? el("img", { src: ph.url, alt: `Camera snapshot: ${e.label}`, class: "event-photo-img" })
      : el("span", { class: "muted" }, ph && ph.state === "missing" ? "No photo"
        : "Loading photo…");
    return el("tr", { class: "event-photo-row" },
      el("td", { colspan: 5, class: "event-photo" }, body));
  }

  async function loadPhoto(id) {
    if (photos.has(id)) return;
    photos.set(id, { state: "loading", url: null });
    let blob = null;
    try {
      blob = await requestBlob(`/snapshots/${encodeURIComponent(id)}`);
    } catch { /* shown as "No photo"; the person can close and reopen to retry */ }
    if (closed) return;
    if (blob) photos.set(id, { state: "ready", url: URL.createObjectURL(blob) });
    else photos.set(id, { state: "missing", url: null });
    renderEvents();
  }

  // --- Away toggle ---
  const guardState = el("span", { class: "guard-state" });
  const guardBtn = el("button", { type: "button", class: "btn btn-quiet guard-btn",
    "aria-pressed": "false" }, "Away");
  const guardNotice = el("p", { class: "guard-notice", role: "status" });
  let noticeTimer = null;
  function setNotice(text, isError = false) {
    guardNotice.className = `guard-notice${isError ? " is-error" : ""}`;
    guardNotice.textContent = text;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { guardNotice.textContent = ""; }, NOTICE_MS);
  }
  function renderGuard() {
    guardBtn.setAttribute("aria-pressed", String(guard.away));
    const hhmm = (t) => String(t).slice(0, 5);
    const night = guard.start && guard.end
      ? ` · night hours ${hhmm(guard.start)}–${hhmm(guard.end)}` : "";
    guardState.textContent = (guard.away ? "Away — guarding" : "Home") + night;
  }
  guardBtn.addEventListener("click", async () => {
    guardBtn.disabled = true;
    try {
      const out = await request("PATCH", "/households/me", { away: !guard.away });
      guard.away = out.away; // the live message adds the "who" notice, for every tab
      renderGuard();
    } catch (err) {
      setNotice(err.detail || err.message, true);
    } finally {
      guardBtn.disabled = false;
    }
  });

  function channelsPanel(p) {
    const on = (v) => el("span", { class: v ? "ch-on" : "ch-off" }, v ? "On" : "Off");
    const tg = p.telegram_linked
      ? on(p.telegram) : el("span", { class: "ch-off" }, "Not linked");
    return el("section", { class: "panel channels", "aria-labelledby": "ch-h" },
      el("h2", { id: "ch-h" }, "How you get alerted"),
      el("div", { class: "ch-row" }, el("span", {}, "Popup in the dashboard"), on(p.popup)),
      el("div", { class: "ch-row" }, el("span", {}, "Alarm sound"), on(p.alarm)),
      el("div", { class: "ch-row" }, el("span", {}, "Telegram"), tg),
      el("div", { class: "ch-row" }, el("span", {}, "ntfy"), on(p.ntfy_topic)));
  }

  function renderAll() {
    renderStatus();
    renderSensors();
    renderEvents();
  }

  // --- live updates ---
  let alertsTimer = null;
  function refreshAlertsSoon() {
    clearTimeout(alertsTimer);
    alertsTimer = setTimeout(async () => {
      try {
        const rows = await request("GET", `/alerts?limit=${CATCH_UP_LIMIT}`);
        if (closed) return;
        addAlerts(rows);
        renderStatus();
        renderEvents();
      } catch { /* the next event or reconnect retries */ }
    }, ALERTS_REFRESH_MS);
  }

  // aria-busy on the feed while a catch-up is in flight (tests and screen readers wait on it).
  let busy = 0;
  function markBusy(delta) {
    busy += delta;
    feed.setAttribute("aria-busy", String(busy > 0));
  }

  // One catch-up at a time; a reconnect during one queues exactly one more.
  let catching = false;
  let again = false;
  async function catchUp() {
    if (catching) {
      again = true;
      return;
    }
    catching = true;
    markBusy(1);
    try {
      do {
        again = false;
        try {
          const [e, a, d] = await Promise.all([
            request("GET", `/events?limit=${CATCH_UP_LIMIT}`),
            request("GET", `/alerts?limit=${CATCH_UP_LIMIT}`),
            request("GET", "/devices"),
          ]);
          if (closed) return;
          addEvents(e);
          addAlerts(a);
          setDevices(d);
          renderAll();
        } catch { /* a 401 already logged out; otherwise the live feed carries on */ }
      } while (again && !closed);
    } finally {
      catching = false;
      markBusy(-1);
    }
  }

  let refreshingDevices = false;
  async function refreshDevices() {
    if (refreshingDevices) return;
    refreshingDevices = true;
    try {
      const d = await request("GET", "/devices");
      if (closed) return;
      setDevices(d);
      renderStatus();
      renderSensors();
    } catch { /* next tick retries */ }
    finally {
      refreshingDevices = false;
    }
  }

  function onMessage(msg) {
    switch (msg.kind) {
      case "event":
        addEvents([{
          id: msg.event_id, label: msg.label, device_name: msg.device_name,
          confidence: msg.confidence, received_at: msg.received_at, merged: msg.merged,
          has_snapshot: Boolean(msg.has_snapshot),
        }]);
        renderStatus();
        renderEvents();
        refreshAlertsSoon(); // the alert rows exist already; the alert message may not come
        break;
      case "alert":
        addAlerts([{ id: msg.alert_id, event_id: msg.event_id, sound_key: msg.sound_key,
          label: msg.label, device_name: msg.device_name, created_at: msg.at }]);
        renderStatus();
        renderEvents();
        break;
      case "device_offline":
      case "device_online": {
        const d = devices.get(msg.device_id);
        if (!d) { refreshDevices(); break; }
        d.status = msg.kind === "device_online" ? "online" : "offline";
        if (msg.kind === "device_online") d.last_seen_at = new Date().toISOString();
        renderStatus();
        renderSensors();
        break;
      }
      case "guarding":
        guard.away = Boolean(msg.away);
        renderGuard();
        setNotice(`${msg.by_email || "Someone"} turned Away ${guard.away ? "on" : "off"}`);
        break;
      default: // alert_ack etc.: handled by later views
    }
  }

  renderAll();
  renderGuard();
  loaded = true;
  for (const msg of early.splice(0)) onMessage(msg);
  // The socket (re)opened while loading, or the buffer overflowed: the fetch may predate
  // messages we never saw, so fetch once more.
  if (earlyOverflow || (status === "live" && liveGen !== liveAtFetch)) catchUp();
  const ticker = setInterval(renderSensors, TICK_MS);
  const devicesTimer = setInterval(refreshDevices, DEVICES_REFRESH_MS);

  const node = el("section", { class: "stack overview" },
    el("header", { class: "page-head" }, el("h1", {}, "Overview"), pill),
    el("div", { class: "guard-bar" }, guardBtn, guardState),
    guardNotice,
    statusBox,
    el("section", { class: "sensors", "aria-labelledby": "sensors-h" },
      el("h2", { id: "sensors-h" }, "Sensors"), sensorGrid),
    el("div", { class: "overview-split" }, feed, channelsPanel(prefs)));

  return {
    node,
    cleanup() {
      closed = true;
      unsubscribe();
      clearInterval(ticker);
      clearInterval(devicesTimer);
      clearTimeout(alertsTimer);
      clearTimeout(noticeTimer);
      for (const ph of photos.values()) if (ph.url) URL.revokeObjectURL(ph.url);
      photos.clear();
    },
  };
}

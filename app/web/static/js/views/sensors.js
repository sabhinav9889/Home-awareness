// Sensors & pairing: the household's sensors, a pairing code for a new one, remove (confirm).
// The list refreshes on device_online/device_offline (shared socket), every 30 s, and every
// 5 s while a pairing code is waiting to be used (pairing itself publishes nothing).
import { request } from "../api.js";
import { subscribe } from "../live.js";
import { confirmDialog, el, field, show } from "../ui.js";
import { ago, KINDS } from "./overview.js";

const CODE_TTL_MS = 10 * 60_000;
const TICK_MS = 1000;
const POLL_PAIRING_MS = 5000;
const POLL_IDLE_MS = 30_000;
const KIND_OPTIONS = [["laptop", "Laptop"], ["phone", "Phone"], ["pi", "Raspberry Pi"],
  ["other", "Other"]];

function mmss(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export async function sensorsView() {
  let closed = false;
  let devices = [];
  let refreshing = false;
  const unsubscribe = subscribe({
    onMessage: (msg) => {
      if (msg.kind === "device_online" || msg.kind === "device_offline") refresh();
    },
  });
  try {
    devices = await request("GET", "/devices");
  } catch (err) {
    unsubscribe();
    throw err;
  }

  const listBox = el("div", { class: "device-list" });
  const listMsg = el("div", { class: "msgs" });
  const countEl = el("span", { class: "muted" });

  async function refresh() {
    if (refreshing || closed) return;
    refreshing = true;
    try {
      const rows = await request("GET", "/devices");
      if (closed) return;
      devices = rows;
      renderList();
    } catch { /* the next tick retries; a 401 already logged out */ }
    finally {
      refreshing = false;
    }
  }

  function stateOf(d) {
    if (d.status === "online") {
      return ["Listening", d.last_seen_at ? `heartbeat ${ago(d.last_seen_at)}` : ""];
    }
    if (d.status === "offline") {
      return ["Offline", d.last_seen_at ? `last heartbeat ${ago(d.last_seen_at)}` : ""];
    }
    return ["Waiting for its first heartbeat", ""];
  }

  function deviceRow(d) {
    const kind = Object.hasOwn(KINDS, d.kind) ? KINDS[d.kind] : "Sensor";
    const [state, when] = stateOf(d);
    const remove = el("button", { type: "button", class: "btn btn-quiet btn-small",
      "aria-label": `Remove ${d.name}` }, "Remove");
    remove.addEventListener("click", async () => {
      const yes = await confirmDialog({
        title: `Remove ${d.name}?`,
        body: "Its key stops working immediately. To use the device again, pair it with a " +
          "new code.",
        confirmLabel: "Remove sensor", danger: true,
      });
      if (!yes || closed) return;
      remove.disabled = true;
      try {
        await request("DELETE", `/devices/${encodeURIComponent(d.id)}`);
        show(listMsg, "");
        devices = devices.filter((x) => x.id !== d.id);
        renderList();
      } catch (err) {
        remove.disabled = false;
        show(listMsg, err.detail || err.message, "error");
      }
    });
    return el("article", { class: `device-row sensor-${d.status}` },
      el("span", { class: "dot", "aria-hidden": "true" }),
      el("div", { class: "device-main" },
        el("div", { class: "sensor-name" }, d.name),
        el("div", { class: "sensor-sub" },
          [kind, el("span", { class: "device-state" }, state), when].filter(Boolean)
            .flatMap((x, i) => (i ? [" · ", x] : [x]))),
        d.status === "offline" ? el("div", { class: "device-note" },
          "Alerts from this sensor have stopped. Check it is powered and on Wi-Fi.") : null),
      remove);
  }

  function renderList() {
    const rows = [...devices].sort((a, b) => a.name.localeCompare(b.name) || a.id - b.id);
    countEl.textContent = ` · ${rows.length}`;
    listBox.replaceChildren(...(rows.length ? rows.map(deviceRow)
      : [el("p", { class: "empty-note" }, "No sensors yet. Add one above.")]));
  }

  // --- pairing ---
  const formMsg = el("div", { class: "msgs" });
  const result = el("div", { class: "pair-result", hidden: true });
  const name = field("Sensor name", { name: "name", maxlength: 80, autocomplete: "off",
    required: true, placeholder: "e.g. Kitchen" });
  const kindSelect = el("select", { id: "pair-kind", name: "kind" },
    KIND_OPTIONS.map(([v, label]) => el("option", { value: v }, label)));
  const submit = el("button", { type: "submit", class: "btn" }, "Create pairing code");
  const form = el("form", { class: "pair-form", novalidate: true },
    name, el("div", { class: "field" }, el("label", { for: "pair-kind" }, "Kind"), kindSelect),
    submit);
  let deadline = 0;
  let expiryEl = null;

  function tick() {
    if (!expiryEl) return;
    const left = deadline - Date.now();
    expiryEl.textContent = left > 0 ? `Expires in ${mmss(left)}`
      : "This code has expired. Create a new one.";
    if (left <= 0) {
      expiryEl.classList.add("is-expired");
      expiryEl = null;
    }
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = name.querySelector("input");
    const value = input.value.trim();
    if (!value) {
      show(formMsg, "Give the sensor a name, like the room it is in.", "error");
      input.focus();
      return;
    }
    submit.disabled = true;
    show(formMsg, "");
    try {
      const out = await request("POST", "/devices/pairing-codes",
        { name: value, kind: kindSelect.value });
      if (closed) return;
      // The server's clock sets expires_at; clamp so a skewed browser clock can't show
      // more than the real 10 minutes (or a negative time).
      const left = Math.min(CODE_TTL_MS, Math.max(0, Date.parse(out.expires_at) - Date.now()));
      deadline = Date.now() + left;
      expiryEl = el("p", { class: "pair-expiry" });
      result.replaceChildren(
        el("p", {}, "On the new device, open the sensor page and enter this code for ",
          el("strong", {}, value), "."),
        el("output", { class: "pair-code",
          "aria-label": `Pairing code: ${String(out.code).split("").join(" ")}` }, out.code),
        expiryEl,
        el("p", { class: "hint-inline" }, "Single use. It shows up below once it is paired."));
      result.hidden = false;
      tick();
      input.value = "";
    } catch (err) {
      show(formMsg, err.detail || err.message, "error");
    } finally {
      submit.disabled = false;
    }
  });

  renderList();
  let ticks = 0;
  const timer = setInterval(() => {
    tick();
    ticks += 1;
    if (ticks % 10 === 0) renderList(); // "12 s ago" stays roughly right
    const pairing = expiryEl !== null;
    if (ticks % ((pairing ? POLL_PAIRING_MS : POLL_IDLE_MS) / TICK_MS) === 0) refresh();
  }, TICK_MS);

  const node = el("section", { class: "stack settings", "data-ready": "true" },
    el("header", { class: "page-head" }, el("h1", {}, "Sensors")),
    el("section", { class: "panel", "aria-labelledby": "pair-h" },
      el("h2", { id: "pair-h" }, "Add a sensor"),
      el("p", { class: "panel-intro" }, "Name it after the room it listens in, then enter the " +
        "code on the new device. The code works once, for 10 minutes."),
      formMsg, form, result),
    el("section", { class: "panel", "aria-labelledby": "list-h" },
      el("h2", { id: "list-h" }, "Your sensors", countEl), listMsg, listBox),
    el("p", { class: "hint-inline" }, "Removing a sensor revokes its key immediately. Sensors " +
      "listen only for sounds someone in your household turned on."));

  return {
    node,
    cleanup() {
      closed = true;
      unsubscribe();
      clearInterval(timer);
    },
  };
}

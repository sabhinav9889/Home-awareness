// Sensors & pairing: the household's sensors, a pairing code for a new one, remove (confirm).
// The list refreshes on device_online/device_offline (shared socket), every 30 s, and every
// 5 s while a pairing code is waiting to be used (pairing itself publishes nothing).
import { request } from "../api.js";
import { subscribe } from "../live.js";
import { confirmDialog, el, field, show } from "../ui.js";
import { ago, KINDS, MIC_CLIPPING_NOTE } from "./overview.js";
import { PRIVATE_KINDS, roomSelect } from "./rooms.js";

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
  let rooms = [];
  let isOwner = false;
  let refreshing = false;
  let listStale = false; // a refresh arrived while focus was inside the list
  const unsubscribe = subscribe({
    onMessage: (msg) => {
      if (msg.kind === "device_online" || msg.kind === "device_offline") refresh();
    },
  });
  try {
    let me;
    [devices, rooms, me] = await Promise.all([
      request("GET", "/devices"), request("GET", "/rooms"), request("GET", "/auth/me"),
    ]);
    isOwner = me.role === "owner";
  } catch (err) {
    unsubscribe();
    throw err;
  }

  const listBox = el("div", { class: "device-list" });
  const listMsg = el("div", { class: "msgs" });
  const countEl = el("span", { class: "muted" });
  listBox.addEventListener("focusout", () => {
    // After the focus move settles: still inside the list (tabbing between its controls)
    // means keep waiting.
    setTimeout(() => {
      if (!listStale || closed || listBox.contains(document.activeElement)) return;
      listStale = false;
      renderList();
    }, 0);
  });

  async function refresh() {
    if (refreshing || closed) return;
    refreshing = true;
    try {
      const [rows, roomRows] = await Promise.all([
        request("GET", "/devices"), request("GET", "/rooms"),
      ]);
      if (closed) return;
      devices = rows;
      rooms = roomRows;
      if (!document.getElementById("cam-room")?.matches(":focus")) renderRoomPick();
      // Never rebuild the list under someone's focus (a select or switch in use): do it
      // when focus leaves instead.
      if (listBox.contains(document.activeElement)) listStale = true;
      else renderList();
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

  const hasVideo = (d) => (d.capabilities || "").split(",").includes("video");

  function cameraExtras(d) {
    const note = el("div", { class: "msgs" });
    let snapshots;
    if (isOwner) {
      snapshots = el("button", { type: "button", role: "switch", class: "switch",
        "aria-checked": String(d.send_snapshots), "aria-label": `Send snapshots from ${d.name}`,
      }, el("span", { class: "knob" }));
      snapshots.addEventListener("click", async () => {
        const next = snapshots.getAttribute("aria-checked") !== "true";
        snapshots.disabled = true;
        try {
          const out = await request("PATCH", `/devices/${encodeURIComponent(d.id)}`,
            { send_snapshots: next });
          d.send_snapshots = out.send_snapshots;
          snapshots.setAttribute("aria-checked", String(d.send_snapshots));
          show(note, "");
        } catch (err) {
          show(note, err.detail || err.message, "error");
        } finally {
          snapshots.disabled = false;
        }
      });
    }
    let consent;
    if (d.consent_confirmed_at) {
      consent = el("div", { class: "cam-consent" }, "Everyone on camera knows · confirmed ",
        new Date(d.consent_confirmed_at).toLocaleDateString(undefined,
          { day: "numeric", month: "short", year: "numeric" }));
    } else {
      const confirm = el("button", { type: "button", class: "btn btn-quiet btn-small" },
        "Confirm everyone knows");
      confirm.addEventListener("click", async () => {
        confirm.disabled = true;
        try {
          await request("PATCH", `/devices/${encodeURIComponent(d.id)}`,
            { consent_confirmed: true });
          await refresh();
        } catch (err) {
          confirm.disabled = false;
          show(note, err.detail || err.message, "error");
        }
      });
      consent = el("div", { class: "cam-consent" }, "Consent not confirmed. ", confirm);
    }
    return el("div", { class: "cam-extras" },
      isOwner ? el("div", { class: "cam-snapshots" }, snapshots,
        el("span", {}, "Send snapshots with alerts"))
        : el("div", { class: "cam-snapshots" },
          `Snapshots: ${d.send_snapshots ? "on" : "off"} (the owner decides)`),
      consent, note);
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
          "Alerts from this sensor have stopped. Check it is powered and on Wi-Fi.") : null,
        d.mic_clipping ? el("div", { class: "device-note is-mic-clipping" }, MIC_CLIPPING_NOTE)
          : null,
        d.room_name ? el("div", { class: "sensor-sub" }, "Room: ", d.room_name) : null,
        hasVideo(d) ? cameraExtras(d) : null,
        roomSelect(d, rooms, {
          onSaved: () => { show(listMsg, ""); refresh(); },
          onError: (m) => show(listMsg, m, "error"),
        })),
      remove);
  }

  function renderList() {
    listStale = false;
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

  // Shows a fresh code in `box` (hiding the other form's), with the live countdown.
  function showCode(box, out, value) {
    const left = Math.min(CODE_TTL_MS, Math.max(0, Date.parse(out.expires_at) - Date.now()));
    deadline = Date.now() + left;
    expiryEl = el("p", { class: "pair-expiry" });
    result.hidden = true;
    camResult.hidden = true;
    box.replaceChildren(
      el("p", {}, "On the new device, open the sensor page and enter this code for ",
        el("strong", {}, value), "."),
      el("output", { class: "pair-code",
        "aria-label": `Pairing code: ${String(out.code).split("").join(" ")}` }, out.code),
      expiryEl,
      el("p", { class: "hint-inline" }, "Single use. It shows up below once it is paired."));
    box.hidden = false;
    tick();
  }

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
      // The server's clock sets expires_at; showCode clamps it so a skewed browser clock
      // can't show more than the real 10 minutes (or a negative time).
      showCode(result, out, value);
      input.value = "";
    } catch (err) {
      show(formMsg, err.detail || err.message, "error");
    } finally {
      submit.disabled = false;
    }
  });

  // --- add a camera ---
  const camMsg = el("div", { class: "msgs", id: "cam-form-msg" });
  const camResult = el("div", { class: "cam-result", id: "cam-form-result", hidden: true });
  const camName = field("Camera name", { name: "camera-name", maxlength: 80,
    autocomplete: "off", placeholder: "e.g. Porch camera" });
  const roomPick = el("select", { id: "cam-room", name: "room" });
  const roomNotice = el("div", { class: "room-notice", role: "status" });
  const consent = el("input", { type: "checkbox", id: "cam-consent", name: "consent" });
  const camSubmit = el("button", { type: "submit", class: "btn" }, "Create camera pairing code");
  const camForm = el("form", { class: "cam-form", novalidate: true }, camName,
    el("div", { class: "field" }, el("label", { for: "cam-room" }, "Room"), roomPick),
    roomNotice,
    el("div", { class: "check-field" }, consent,
      el("label", { for: "cam-consent" },
        "Everyone who may be on this camera knows about it")),
    camSubmit);

  function renderRoomPick() {
    const current = roomPick.value;
    roomPick.replaceChildren(
      el("option", { value: "" }, rooms.length ? "Choose a room" : "No rooms yet"),
      ...rooms.map((r) => el("option", { value: String(r.id) }, r.name)));
    roomPick.value = rooms.some((r) => String(r.id) === current) ? current : "";
    showRoomNotice();
  }

  function showRoomNotice() {
    const room = rooms.find((r) => String(r.id) === roomPick.value);
    if (!rooms.length) {
      roomNotice.className = "room-notice";
      roomNotice.replaceChildren("Create a room first, in ",
        el("a", { href: "#/rooms" }, "Rooms"), ".");
    } else if (room && PRIVATE_KINDS.has(room.kind)) {
      roomNotice.className = "room-notice is-private";
      roomNotice.replaceChildren(el("strong", {}, "This is a private room. "),
        "Think hard before putting a camera here: everyone who uses the room must agree. " +
        "Photos will be off by default for a camera in this room; the owner can turn them on.");
    } else {
      roomNotice.className = "room-notice";
      roomNotice.replaceChildren("The camera sends one photo only when it raises an alert.");
    }
  }
  roomPick.addEventListener("change", showRoomNotice);

  camForm.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const input = camName.querySelector("input");
    const value = input.value.trim();
    const problem = !value ? ["Give the camera a name, like the spot it watches.", input]
      : !roomPick.value ? ["Choose the room this camera is in.", roomPick]
        : !consent.checked
          ? ["Confirm that everyone who may be on this camera knows about it.", consent]
          : null;
    if (problem) {
      show(camMsg, problem[0], "error");
      problem[1].focus();
      return;
    }
    camSubmit.disabled = true;
    show(camMsg, "");
    try {
      const out = await request("POST", "/devices/pairing-codes", {
        name: value, kind: "camera", capabilities: "video", consent_confirmed: true,
        room_id: Number(roomPick.value),
      });
      if (closed) return;
      showCode(camResult, out, value);
      input.value = "";
      consent.checked = false;
    } catch (err) {
      show(camMsg, err.detail || err.message, "error");
    } finally {
      camSubmit.disabled = false;
    }
  });

  renderRoomPick();
  renderList();
  let ticks = 0;
  const timer = setInterval(() => {
    tick();
    ticks += 1;
    // "12 s ago" stays roughly right (not while a select in the list is being used)
    if (ticks % 10 === 0 && !listBox.contains(document.activeElement)) renderList();
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
    el("section", { class: "panel", "aria-labelledby": "cam-h" },
      el("h2", { id: "cam-h" }, "Add a camera"),
      el("p", { class: "panel-intro" }, "Cameras see people, so choose the room and confirm " +
        "everyone knows. The video stays on the camera: only a single photo is sent, and " +
        "only when it raises an alert."),
      camMsg, camForm, camResult),
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

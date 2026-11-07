// Rooms: the household's rooms and the sensors in each. The owner creates, renames and
// deletes rooms; any member can move a sensor to a room. Room names are text only.
import { request } from "../api.js";
import { confirmDialog, el, field, show } from "../ui.js";

export const ROOM_KINDS = [
  ["common", "Living area"], ["entrance", "Entrance"], ["bedroom", "Bedroom"],
  ["bathroom", "Bathroom"], ["outdoor", "Outdoor"], ["other", "Other"],
];
export const PRIVATE_KINDS = new Set(["bedroom", "bathroom"]);

const kindLabel = (k) => (ROOM_KINDS.find(([v]) => v === k) || [0, "Other"])[1];

// A "Room for <device>" select that saves on change. `onSaved(updatedDevice)` after success,
// `onError(message)` on failure (the select goes back to the saved room).
export function roomSelect(device, rooms, { onSaved, onError }) {
  const select = el("select", { class: "room-select", "aria-label": `Room for ${device.name}` },
    el("option", { value: "" }, "No room"),
    rooms.map((r) => el("option", { value: String(r.id) }, r.name)));
  select.value = device.room_id == null ? "" : String(device.room_id);
  select.addEventListener("change", async () => {
    const value = select.value;
    select.disabled = true;
    try {
      const out = await request("PATCH", `/devices/${encodeURIComponent(device.id)}`,
        { room_id: value === "" ? null : Number(value) });
      onSaved(out);
    } catch (err) {
      select.value = device.room_id == null ? "" : String(device.room_id);
      onError(err.detail || err.message);
    } finally {
      select.disabled = false;
    }
  });
  return select;
}

export async function roomsView() {
  let closed = false;
  const [me, loadedRooms, loadedDevices] = await Promise.all([
    request("GET", "/auth/me"), request("GET", "/rooms"), request("GET", "/devices"),
  ]);
  const isOwner = me.role === "owner";
  let rooms = loadedRooms;
  let devices = loadedDevices;
  let renaming = null; // room id being renamed

  const listBox = el("div", { class: "room-list" });
  const listMsg = el("div", { class: "msgs" });

  async function reload() {
    [rooms, devices] = await Promise.all([request("GET", "/rooms"), request("GET", "/devices")]);
    if (!closed) renderList();
  }

  function deviceLine(d) {
    return el("li", { class: "room-device" },
      el("span", { class: "room-device-name" }, d.name),
      roomSelect(d, rooms, {
        onSaved: () => { show(listMsg, ""); reload().catch(() => {}); },
        onError: (m) => show(listMsg, m, "error"),
      }));
  }

  function roomCard(room) {
    const mine = devices.filter((d) => d.room_id === room.id);
    let head;
    if (renaming === room.id) {
      const input = el("input", { value: room.name, maxlength: 60, autocomplete: "off",
        "aria-label": `New name for ${room.name}` });
      const save = el("button", { type: "button", class: "btn btn-small" }, "Save name");
      const cancel = el("button", { type: "button", class: "btn btn-quiet btn-small" }, "Cancel");
      cancel.addEventListener("click", () => { renaming = null; renderList(); });
      save.addEventListener("click", async () => {
        const name = input.value.trim();
        if (!name) {
          show(listMsg, "A room needs a name.", "error");
          return;
        }
        save.disabled = true;
        try {
          await request("PATCH", `/rooms/${encodeURIComponent(room.id)}`, { name });
          show(listMsg, "");
          renaming = null;
          await reload();
        } catch (err) {
          save.disabled = false;
          show(listMsg, err.detail || err.message, "error");
        }
      });
      head = el("div", { class: "room-head room-rename" }, input, save, cancel);
      queueMicrotask(() => input.focus());
    } else {
      const actions = [];
      if (isOwner) {
        const rename = el("button", { type: "button", class: "btn btn-quiet btn-small",
          "aria-label": `Rename ${room.name}` }, "Rename");
        rename.addEventListener("click", () => { renaming = room.id; renderList(); });
        const del = el("button", { type: "button", class: "btn btn-quiet btn-small",
          "aria-label": `Delete ${room.name}` }, "Delete");
        del.addEventListener("click", async () => {
          const yes = await confirmDialog({
            title: `Delete ${room.name}?`,
            body: "Its sensors stay paired but are no longer assigned to a room.",
            confirmLabel: "Delete room", danger: true,
          });
          if (!yes || closed) return;
          try {
            await request("DELETE", `/rooms/${encodeURIComponent(room.id)}`);
            show(listMsg, "");
            await reload();
          } catch (err) {
            show(listMsg, err.detail || err.message, "error");
          }
        });
        actions.push(rename, del);
      }
      head = el("div", { class: "room-head" },
        el("div", { class: "device-main" },
          el("h3", { class: "room-name" }, room.name),
          el("div", { class: "sensor-sub" }, kindLabel(room.kind))),
        actions.length ? el("div", { class: "ch-actions" }, actions) : null);
    }
    return el("article", { class: "room-card" }, head,
      mine.length ? el("ul", { class: "room-devices" }, mine.map(deviceLine))
        : el("p", { class: "empty-note" }, "No sensors in this room."));
  }

  function renderList() {
    const loose = devices.filter((d) => d.room_id == null);
    listBox.replaceChildren(
      ...(rooms.length ? rooms.map(roomCard)
        : [el("p", { class: "empty-note" }, isOwner ? "No rooms yet. Add one above."
          : "No rooms yet. The owner can add them.")]),
      loose.length ? el("article", { class: "room-card room-unassigned" },
        el("h3", { class: "room-name" }, "Not in a room"),
        el("ul", { class: "room-devices" }, loose.map(deviceLine))) : null);
  }

  // --- add a room (owner) ---
  let addPanel = null;
  if (isOwner) {
    const msg = el("div", { class: "msgs" });
    const name = field("Room name", { name: "name", maxlength: 60, autocomplete: "off",
      placeholder: "e.g. Kitchen" });
    const kind = el("select", { id: "room-kind", name: "kind" },
      ROOM_KINDS.map(([v, label]) => el("option", { value: v }, label)));
    const add = el("button", { type: "submit", class: "btn" }, "Add room");
    const form = el("form", { class: "pair-form", novalidate: true }, name,
      el("div", { class: "field" }, el("label", { for: "room-kind" }, "Room type"), kind), add);
    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const input = name.querySelector("input");
      const value = input.value.trim();
      if (!value) {
        show(msg, "Give the room a name.", "error");
        input.focus();
        return;
      }
      add.disabled = true;
      show(msg, "");
      try {
        await request("POST", "/rooms", { name: value, kind: kind.value });
        input.value = "";
        await reload();
      } catch (err) {
        show(msg, err.detail || err.message, "error");
      } finally {
        add.disabled = false;
      }
    });
    addPanel = el("section", { class: "panel", "aria-labelledby": "room-add-h" },
      el("h2", { id: "room-add-h" }, "Add a room"),
      el("p", { class: "panel-intro" }, "Rooms group sensors and cameras. Cameras in a " +
        "bedroom or bathroom start with photos off."),
      msg, form);
  }

  renderList();
  const node = el("section", { class: "stack settings", "data-ready": "true" },
    el("header", { class: "page-head" }, el("h1", {}, "Rooms")),
    addPanel,
    el("section", { class: "panel", "aria-labelledby": "room-list-h" },
      el("h2", { id: "room-list-h" }, "Your rooms"), listMsg, listBox));
  return { node, cleanup() { closed = true; } };
}

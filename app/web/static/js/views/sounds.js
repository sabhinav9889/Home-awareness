// Sounds & rules: per-member switches and thresholds, saved per row (debounced).
import { request } from "../api.js";
import { el } from "../ui.js";

const DEBOUNCE_MS = 400;
let seq = 0;

// FastAPI details can be a string or a pydantic list; ApiError.detail joins the messages.
function firstMessage(err) {
  return String(err.detail || err.message).split("; ")[0];
}

function numberField(label, attrs, onInput) {
  const id = `snd${++seq}`;
  const input = el("input", { id, type: "number", inputmode: "decimal", ...attrs });
  input.addEventListener("input", onInput);
  return { input, node: el("div", { class: "snd-field" }, el("label", { for: id }, label), input) };
}

function soundRow(sound, setting) {
  let timer = null;
  let confTouched = false; // only send min_confidence once the user changed it
  let clearConf = false;
  let enabled = setting.enabled;
  const errorBox = el("div", { class: "row-error-slot" });

  const sw = el("button", {
    type: "button", role: "switch", class: "switch", "aria-checked": String(enabled),
    "aria-label": `Alert me for ${sound.label}`,
  }, el("span", { class: "knob" }));

  async function save() {
    timer = null;
    const body = {
      enabled,
      repeat_count: Number(repeat.input.value),
      window_s: Number(win.input.value),
      cooldown_s: Math.round(Number(quiet.input.value) * 60),
    };
    if (clearConf) body.min_confidence = null;
    else if (confTouched) body.min_confidence = Number(conf.input.value);
    try {
      const out = await request("PUT", `/me/sound-settings/${encodeURIComponent(sound.key)}`, body);
      errorBox.replaceChildren();
      if (clearConf) {
        conf.input.value = String(out.min_confidence);
        confTouched = clearConf = false;
      }
      syncReset(out.min_confidence);
    } catch (err) {
      errorBox.replaceChildren(el("p", { class: "row-error", role: "alert" }, firstMessage(err)));
    }
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(save, DEBOUNCE_MS);
  }

  const repeat = numberField("Alert after (times)",
    { min: 1, max: 20, step: 1, value: setting.repeat_count }, schedule);
  const win = numberField("Within (seconds)",
    { min: 5, max: 3600, step: 1, value: setting.window_s }, schedule);
  const quiet = numberField("Quiet for (minutes)",
    { min: 0, max: 1440, step: "any", value: setting.cooldown_s / 60 }, schedule);
  const conf = numberField("Min confidence",
    { min: 0.05, max: 0.95, step: 0.05, value: setting.min_confidence }, () => {
      confTouched = true;
      clearConf = false;
      syncReset(Number(conf.input.value));
      schedule();
    });

  const reset = el("button", {
    type: "button", class: "link-btn", hidden: true,
    onclick: () => { clearConf = true; confTouched = false; reset.hidden = true; clearTimeout(timer); save(); },
  }, "Reset");
  function syncReset(value) {
    reset.hidden = Math.abs(value - sound.default_min_confidence) < 1e-9;
  }
  syncReset(setting.min_confidence);

  sw.addEventListener("click", () => {
    enabled = !enabled;
    sw.setAttribute("aria-checked", String(enabled));
    schedule();
  });

  return el("div", { class: "snd-row", "data-sound": sound.key },
    el("div", { class: "snd-head" }, sw,
      el("span", { class: "snd-label" }, sound.label),
      el("span", { class: `badge badge-rel badge-${sound.reliability}` }, sound.reliability),
      sound.severity === "urgent" ? el("span", { class: "badge badge-urgent" }, "Urgent") : null),
    el("div", { class: "snd-fields" }, repeat.node, win.node, quiet.node,
      el("div", { class: "snd-field" }, conf.node.firstChild, conf.input, reset)),
    errorBox);
}

export async function soundsView() {
  const [sounds, settings] = await Promise.all([
    request("GET", "/sounds"), request("GET", "/me/sound-settings"),
  ]);
  const byKey = new Map(settings.map((s) => [s.sound_key, s]));
  const groups = new Map();
  for (const s of sounds) {
    if (!groups.has(s.group)) groups.set(s.group, []);
    groups.get(s.group).push(s);
  }
  return el("section", { class: "sounds" },
    el("h1", {}, "Sounds & rules"),
    el("p", { class: "snd-intro" },
      "Sensors only listen for sounds someone in your household turned on. ",
      "Your settings only: other members choose their own."),
    [...groups].map(([name, items]) => el("section", { class: "panel snd-group" },
      el("h2", { class: "snd-group-title" }, name),
      items.filter((s) => byKey.has(s.key)).map((s) => soundRow(s, byKey.get(s.key))))),
    el("p", { class: "hint snd-note" },
      "Reliability labels come from testing each sound on recorded clips. ",
      "Experimental sounds may miss events or fire falsely."));
}

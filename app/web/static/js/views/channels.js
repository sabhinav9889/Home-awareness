// Alert channels: popup / alarm / Telegram / ntfy for me, a test alert, browser notification
// permission, and my laptop agents. Every PUT re-reads the preferences first and sends the
// full body from that fresh state, so a change made in another tab is never overwritten.
// Saves run one at a time. A re-render keeps keyboard focus on the same control (data-key),
// or on the first control of the same channel (data-row) if that one is gone.
import { request } from "../api.js";
import { confirmDialog, copyText, el, show } from "../ui.js";
import { ago } from "./overview.js";

const NOT_CONFIGURED = "Telegram is not configured";

function telegramUrl(value) {
  if (typeof value !== "string" || !value.startsWith("https://t.me/")) return null;
  try {
    const u = new URL(value);
    return u.protocol === "https:" && u.host === "t.me" ? u.href : null;
  } catch {
    return null;
  }
}

function permissionText(p) {
  if (p === "granted") return "Browser notifications are allowed. You get one when an alert " +
    "arrives while this tab is in the background.";
  if (p === "denied") return "Browser notifications are blocked. Allow them in this site's " +
    "settings in your browser, then reload.";
  return "Not enabled yet: a background tab stays silent — you'll only see the alert when " +
    "you switch back.";
}

export async function channelsView() {
  let closed = false;
  let [prefs, agents] = await Promise.all([
    request("GET", "/me/alert-preferences"), request("GET", "/me/agent-tokens"),
  ]);

  const prefsMsg = el("div", { class: "msgs" });
  let chain = Promise.resolve();
  // Run one change at a time: fn() gets nothing and returns the new preferences.
  function change(fn) {
    chain = chain.then(async () => {
      if (closed) return;
      try {
        const out = await fn();
        if (closed) return;
        prefs = out;
        show(prefsMsg, "");
      } catch (err) {
        show(prefsMsg, err.detail || err.message, "error");
      }
      render();
    });
    return chain;
  }
  async function put(changes) {
    const fresh = await request("GET", "/me/alert-preferences");
    return request("PUT", "/me/alert-preferences", {
      popup: fresh.popup, alarm: fresh.alarm, telegram: fresh.telegram,
      ntfy_topic: fresh.ntfy_topic, ...changes,
    });
  }

  // Focus to restore after a re-render: set when a control in the list is used, because a
  // control that is disabled (or replaced) while its save runs loses focus to <body>.
  let refocus = null; // {key, row}
  function remember(node) {
    const active = document.activeElement;
    if (!active || active === document.body || !list.contains(active)) return;
    const row = active.closest("[data-row]");
    refocus = { key: active.dataset.key || node?.dataset.key, row: row && row.dataset.row };
  }
  function restoreFocus() {
    if (!refocus) return;
    const active = document.activeElement;
    if (active && active !== document.body && !list.contains(active)) {
      refocus = null; // the person has moved on
      return;
    }
    const usable = (n) => n && !n.disabled && n.isConnected;
    let target = list.querySelector(`[data-key="${refocus.key}"]`);
    if (target && target.disabled) return; // still saving: try again after it re-enables
    if (!usable(target) && refocus.row) {
      target = [...list.querySelectorAll(`[data-row="${refocus.row}"] button, ` +
        `[data-row="${refocus.row}"] a[href]`)].find(usable);
    }
    refocus = null;
    if (usable(target) && target !== active) target.focus();
  }

  function switchFor(key, label) {
    const sw = el("button", { type: "button", role: "switch", class: "switch",
      "aria-label": label, "data-key": `sw-${key}` }, el("span", { class: "knob" }));
    sw.addEventListener("click", () => {
      const next = !prefs[key];
      sw.setAttribute("aria-checked", String(next));
      sw.disabled = true;
      change(() => put({ [key]: next })).then(() => {
        sw.disabled = false;
        restoreFocus();
      });
    });
    return sw;
  }
  const popupSw = switchFor("popup", "Popup");
  const alarmSw = switchFor("alarm", "Alarm sound");
  const telegramSw = switchFor("telegram", "Telegram");

  function row(id, title, desc, control, ...extra) {
    return el("div", { class: "ch-item", "data-row": id },
      el("div", { class: "ch-text" },
        el("div", { class: "ch-title" }, title),
        el("div", { class: "ch-desc" }, desc), ...extra),
      control);
  }

  // --- Telegram ---
  let telegramUnavailable = false;
  let deepLink = null;
  const tgMsg = el("div", { class: "msgs" });

  async function linkTelegram(button) {
    button.disabled = true;
    show(tgMsg, "");
    try {
      const out = await request("POST", "/me/telegram/link");
      deepLink = telegramUrl(out && out.deep_link);
      if (!deepLink) show(tgMsg, "The server sent an unexpected Telegram link.", "error");
    } catch (err) {
      if (err.status === 400 && err.detail === NOT_CONFIGURED) {
        telegramUnavailable = true;
      } else {
        show(tgMsg, err.status === 429 && err.retryAfter
          ? `Too many links. Try again in ${err.retryAfter} s.` : err.detail || err.message,
        "error");
      }
    }
    if (!closed) render();
  }

  function telegramExtra() {
    if (prefs.telegram_linked) {
      const unlink = el("button", { type: "button", class: "link-btn", "data-key": "tg-unlink" },
        "Unlink");
      unlink.addEventListener("click", () => {
        unlink.disabled = true;
        change(async () => {
          await request("DELETE", "/me/telegram");
          return request("GET", "/me/alert-preferences");
        });
      });
      return [el("div", { class: "ch-status" }, "Linked to your Telegram account. ", unlink)];
    }
    const button = el("button", { type: "button", class: "btn btn-quiet btn-small",
      disabled: telegramUnavailable, "data-key": "tg-link" }, "Link Telegram");
    button.addEventListener("click", () => linkTelegram(button));
    const parts = [el("div", { class: "ch-actions" }, button), tgMsg];
    if (telegramUnavailable) {
      parts.push(el("p", { class: "ch-note" }, "Telegram isn't set up on this server. " +
        "Whoever runs it can add a bot (see the README)."));
    } else if (deepLink) {
      const check = el("button", { type: "button", class: "link-btn", "data-key": "tg-check" },
        "I pressed Start");
      check.addEventListener("click", () => {
        change(() => request("GET", "/me/alert-preferences")).then(() => {
          if (!prefs.telegram_linked) show(tgMsg, "Not linked yet: open the link and press " +
            "Start in Telegram.");
          else deepLink = null;
        });
      });
      parts.push(el("p", { class: "ch-note" },
        el("a", { href: deepLink, target: "_blank", rel: "noopener noreferrer" },
          "Open Telegram"),
        ", press Start, then come back: ", check, ". The link works once, for 10 minutes."));
    }
    return parts;
  }

  // --- ntfy ---
  const ntfyMsg = el("div", { class: "msgs" });
  function generateTopic(replace) {
    return async () => {
      if (replace && !(await confirmDialog({
        title: "Replace your ntfy topic?",
        body: "Alerts go to the new topic only. Subscribe to it in the ntfy app again.",
        confirmLabel: "New topic",
      }))) return;
      change(async () => {
        await request("POST", "/me/alert-preferences/ntfy-topic");
        return request("GET", "/me/alert-preferences");
      });
    };
  }
  function ntfyExtra() {
    if (!prefs.ntfy_topic) {
      const gen = el("button", { type: "button", class: "btn btn-quiet btn-small",
        "data-key": "ntfy-generate" }, "Generate topic");
      gen.addEventListener("click", generateTopic(false));
      return [el("div", { class: "ch-actions" }, gen)];
    }
    const topic = prefs.ntfy_topic;
    const copy = el("button", { type: "button", class: "btn btn-quiet btn-small",
      "data-key": "ntfy-copy" }, "Copy");
    copy.addEventListener("click", async () => show(ntfyMsg, await copyText(topic)));
    const again = el("button", { type: "button", class: "btn btn-quiet btn-small",
      "data-key": "ntfy-new" }, "New topic");
    again.addEventListener("click", generateTopic(true));
    const remove = el("button", { type: "button", class: "btn btn-quiet btn-small",
      "data-key": "ntfy-remove" }, "Remove topic");
    remove.addEventListener("click", () => {
      remove.disabled = true;
      change(() => put({ ntfy_topic: null }));
    });
    return [
      el("div", { class: "ch-status" }, "Subscribe to this secret topic in the ntfy app:"),
      el("code", { class: "ntfy-topic mono" }, topic),
      el("div", { class: "ch-actions" }, copy, again, remove), ntfyMsg,
    ];
  }

  const list = el("div", { class: "ch-list" });
  list.addEventListener("click", (ev) => remember(ev.target.closest("[data-key]")), true);
  function render() {
    popupSw.setAttribute("aria-checked", String(prefs.popup));
    alarmSw.setAttribute("aria-checked", String(prefs.alarm));
    telegramSw.setAttribute("aria-checked", String(prefs.telegram));
    list.replaceChildren(
      row("popup", "Popup", "On the dashboard and on your laptop, even with music playing.", popupSw),
      row("alarm", "Alarm sound", "Plays through whatever you are listening on, so your headphones " +
        "when they are in.", alarmSw),
      row("telegram", "Telegram", "A push on your phone. Works when your laptop is off.",
        prefs.telegram_linked ? telegramSw : null, ...telegramExtra()),
      row("ntfy", "ntfy", "Push through the free ntfy app, on a secret topic only you know.",
        null, ...ntfyExtra()));
    restoreFocus();
  }
  render();

  // --- test alert ---
  const testMsg = el("p", { class: "test-alert-msg", role: "status" });
  const testBtn = el("button", { type: "button", class: "btn" }, "Send me a test alert");
  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    testMsg.textContent = "";
    testMsg.classList.remove("is-error");
    try {
      await request("POST", "/me/test-alert");
      testMsg.textContent = "Sent. It arrives on every channel you turned on.";
    } catch (err) {
      testMsg.classList.add("is-error");
      testMsg.textContent = err.status === 429
        ? `Too many test alerts. Try again in ${err.retryAfter ?? 60} s.`
        : err.detail || err.message;
    } finally {
      testBtn.disabled = false;
    }
  });

  // --- browser notifications ---
  const supported = "Notification" in window;
  const notifyMsg = el("p", { class: "notify-msg", role: "status" },
    supported ? permissionText(Notification.permission)
      : "This browser doesn't support notifications.");
  const notifyBtn = el("button", { type: "button", class: "btn btn-quiet",
    disabled: !supported }, "Enable browser notifications");
  notifyBtn.addEventListener("click", async () => {
    try {
      const result = await Notification.requestPermission();
      notifyMsg.textContent = permissionText(result);
    } catch {
      notifyMsg.textContent = "This browser didn't let the page ask.";
    }
  });

  // --- laptop agents ---
  const agentsBox = el("div", { class: "agent-list" });
  const agentsMsg = el("div", { class: "msgs" });
  function renderAgents() {
    agentsBox.replaceChildren(...(agents.length ? agents.map((a) => {
      const revoke = el("button", { type: "button", class: "btn btn-quiet btn-small",
        "aria-label": `Revoke ${a.name}` }, "Revoke");
      revoke.addEventListener("click", async () => {
        const yes = await confirmDialog({
          title: `Revoke ${a.name}?`,
          body: "ha-agent on that laptop can't connect again until you sign in there anew.",
          confirmLabel: "Revoke", danger: true,
        });
        if (!yes || closed) return;
        revoke.disabled = true;
        try {
          await request("DELETE", `/me/agent-tokens/${encodeURIComponent(a.id)}`);
          agents = agents.filter((x) => x.id !== a.id);
          show(agentsMsg, "");
          renderAgents();
        } catch (err) {
          revoke.disabled = false;
          show(agentsMsg, err.detail || err.message, "error");
        }
      });
      return el("div", { class: "agent-row" },
        el("div", { class: "device-main" },
          el("div", { class: "sensor-name" }, a.name),
          el("div", { class: "sensor-sub" },
            a.last_used_at ? `Last used ${ago(a.last_used_at)}` : "Not used yet")),
        revoke);
    }) : [el("p", { class: "empty-note" },
      "No laptop agents. ha-agent adds itself here when you sign in to it on a laptop.")]));
  }
  renderAgents();

  const node = el("section", { class: "stack settings", "data-ready": "true" },
    el("header", { class: "page-head" }, el("h1", {}, "Alert channels")),
    el("section", { class: "panel", "aria-labelledby": "how-h" },
      el("h2", { id: "how-h" }, "How I get alerted"),
      el("p", { class: "panel-intro" },
        "Pick any mix. These are yours only; other members set their own."),
      prefsMsg, list),
    el("section", { class: "panel", "aria-labelledby": "test-h" },
      el("h2", { id: "test-h" }, "Check it works"),
      el("div", { class: "ch-actions" }, testBtn), testMsg,
      el("p", { class: "hint-inline" },
        "Delivery is best effort. Home-awareness tool, not an emergency service.")),
    el("section", { class: "panel", "aria-labelledby": "bn-h" },
      el("h2", { id: "bn-h" }, "Browser notifications"),
      el("div", { class: "ch-actions" }, notifyBtn), notifyMsg),
    el("section", { class: "panel", "aria-labelledby": "ag-h" },
      el("h2", { id: "ag-h" }, "Laptop agents"), agentsMsg, agentsBox));

  return { node, cleanup() { closed = true; } };
}

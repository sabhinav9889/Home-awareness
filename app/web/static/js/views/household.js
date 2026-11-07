// Household: name, timezone, members. Owner-only controls (timezone, invites, removing
// members, deleting the household) are not rendered at all for members; members get "Leave".
// After a delete or leave the session is gone, so the view logs out with a message. It stops
// the shared socket first: the server closes it (4401/4403) for everyone removed, and that
// would otherwise log out with the generic "session ended" message instead.
import { request } from "../api.js";
import * as auth from "../auth.js";
import { startLive, stopLive } from "../live.js";
import { confirmDialog, copyText, el, show } from "../ui.js";

const ROLES = { owner: "Owner", member: "Member" };

function zones(current) {
  const all = new Set(Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []);
  all.add("UTC");
  all.add(current);
  return [...all].sort();
}

function dateText(iso) {
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "long", year: "numeric" })
    .format(new Date(iso));
}

export async function householdView() {
  let closed = false;
  let [household, me] = await Promise.all([
    request("GET", "/households/me"), request("GET", "/auth/me"),
  ]);
  const isOwner = me.role === "owner";

  // --- timezone ---
  const tzMsg = el("p", { class: "tz-msg", role: "status" });
  let tzControl;
  if (isOwner) {
    const select = el("select", { id: "hh-tz", name: "timezone" },
      zones(household.timezone).map((z) => el("option", { value: z }, z)));
    select.value = household.timezone;
    select.addEventListener("change", async () => {
      const value = select.value;
      select.disabled = true;
      tzMsg.classList.remove("is-error");
      tzMsg.textContent = "";
      try {
        household = await request("PATCH", "/households/me", { timezone: value });
        tzMsg.textContent = "Timezone saved.";
      } catch (err) {
        select.value = household.timezone;
        tzMsg.classList.add("is-error");
        tzMsg.textContent = err.detail || err.message;
      } finally {
        select.disabled = false;
      }
    });
    tzControl = el("div", { class: "field" }, el("label", { for: "hh-tz" }, "Timezone"), select,
      el("p", { class: "hint-inline" }, "Event and alert times use this timezone."));
  } else {
    tzControl = el("p", {}, "Timezone: ", el("strong", {}, household.timezone),
      ". The owner can change it.");
  }

  // --- members ---
  const membersBox = el("div", { class: "member-list" });
  const membersMsg = el("div", { class: "msgs" });
  function renderMembers() {
    membersBox.replaceChildren(...household.members.map((m) => {
      let remove = null;
      if (isOwner && m.id !== me.id) {
        remove = el("button", { type: "button", class: "btn btn-quiet btn-small",
          "aria-label": `Remove ${m.email}` }, "Remove");
        remove.addEventListener("click", async () => {
          const yes = await confirmDialog({
            title: `Remove ${m.email}?`,
            body: "They are signed out at once and lose access to this household.",
            confirmLabel: "Remove member", danger: true,
          });
          if (!yes || closed) return;
          remove.disabled = true;
          try {
            await request("DELETE", `/households/me/members/${encodeURIComponent(m.id)}`);
            household = await request("GET", "/households/me");
            show(membersMsg, "");
            if (!closed) renderMembers();
          } catch (err) {
            remove.disabled = false;
            show(membersMsg, err.detail || err.message, "error");
          }
        });
      }
      const role = Object.hasOwn(ROLES, m.role) ? ROLES[m.role] : "Member";
      return el("div", { class: "member-row" },
        el("div", { class: "device-main" },
          el("div", { class: "member-email" }, m.email, m.id === me.id ? " (you)" : ""),
        ),
        el("span", { class: `chip chip-role-${m.role === "owner" ? "owner" : "member"}` }, role),
        remove);
    }));
  }
  renderMembers();

  // --- invite (owner) ---
  let invitePanel = null;
  if (isOwner) {
    const inviteMsg = el("div", { class: "msgs" });
    const inviteOut = el("div", { class: "invite-out" });
    const create = el("button", { type: "button", class: "btn" }, "Create invite link");
    create.addEventListener("click", async () => {
      create.disabled = true;
      show(inviteMsg, "");
      try {
        const out = await request("POST", "/households/me/invites");
        if (closed) return;
        const url = String(out.invite_url);
        // Shown as text only (never a link). Warn if it doesn't point at this dashboard:
        // the server's PUBLIC_BASE_URL is then wrong and the link won't work.
        if (!url.startsWith(`${location.origin}/app/`)) {
          show(inviteMsg, "This link doesn't point at this dashboard, so it may not work. " +
            "Whoever runs the server should check PUBLIC_BASE_URL.", "error");
        }
        const input = el("input", { id: "hh-invite", readonly: true, value: url,
          class: "mono" });
        const copy = el("button", { type: "button", class: "btn btn-quiet" }, "Copy");
        const copyMsg = el("div", { class: "msgs" });
        copy.addEventListener("click", async () => show(copyMsg, await copyText(url, input)));
        inviteOut.replaceChildren(
          el("div", { class: "field" }, el("label", { for: "hh-invite" }, "Invite link"),
            el("div", { class: "copy-row" }, input, copy)),
          copyMsg,
          el("p", { class: "hint-inline" },
            `Works once, until ${dateText(out.expires_at)}. Send it to one person you trust.`));
      } catch (err) {
        show(inviteMsg, err.detail || err.message, "error");
      } finally {
        create.disabled = false;
      }
    });
    invitePanel = el("section", { class: "panel", "aria-labelledby": "inv-h" },
      el("h2", { id: "inv-h" }, "Invite someone"),
      el("p", { class: "panel-intro" }, "Each link adds one member, who chooses their own " +
        "sounds and alert channels."),
      el("div", { class: "ch-actions" }, create), inviteMsg, inviteOut);
  }

  // --- leave / delete ---
  const endMsg = el("div", { class: "msgs" });
  async function endSession(call, message) {
    stopLive();
    try {
      await call();
    } catch (err) {
      startLive();
      show(endMsg, err.detail || err.message, "error");
      return;
    }
    auth.logout(message);
  }
  let endButton;
  if (isOwner) {
    endButton = el("button", { type: "button", class: "btn btn-danger" }, "Delete household");
    endButton.addEventListener("click", async () => {
      const yes = await confirmDialog({
        title: "Delete household?",
        body: "This deletes every sensor, event, alert and member account in it. It can't " +
          "be undone.",
        confirmLabel: "Delete household", danger: true,
        requireText: household.name, requireLabel: "Type the household name to confirm",
      });
      if (yes && !closed) {
        await endSession(() => request("DELETE", "/households/me"),
          "Your household was deleted.");
      }
    });
  } else {
    endButton = el("button", { type: "button", class: "btn btn-danger" }, "Leave household");
    endButton.addEventListener("click", async () => {
      const yes = await confirmDialog({
        title: "Leave household?",
        body: "You stop getting its alerts and your account is removed. To come back you " +
          "need a new invite.",
        confirmLabel: "Leave household", danger: true,
      });
      if (yes && !closed) {
        await endSession(() => request("POST", "/households/me/leave"),
          "You left the household.");
      }
    });
  }

  const node = el("section", { class: "stack settings", "data-ready": "true" },
    el("header", { class: "page-head" }, el("h1", {}, "Household")),
    el("section", { class: "panel", "aria-labelledby": "hh-name" },
      el("h2", { id: "hh-name", class: "hh-name" }, household.name), tzControl, tzMsg),
    el("section", { class: "panel", "aria-labelledby": "mem-h" },
      el("h2", { id: "mem-h" }, "Members"), membersMsg, membersBox),
    invitePanel,
    el("section", { class: "panel panel-danger", "aria-labelledby": "end-h" },
      el("h2", { id: "end-h" }, isOwner ? "Delete household" : "Leave household"),
      el("div", { class: "ch-actions" }, endButton), endMsg));

  return { node, cleanup() { closed = true; } };
}

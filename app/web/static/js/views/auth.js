// Public views: log in, sign up, confirm email, reset password.
import * as auth from "../auth.js";
import { navigate } from "../router.js";
import { el, field, message, show } from "../ui.js";

function card(title, ...children) {
  return el("section", { class: "card auth-card", "aria-labelledby": "auth-h" },
    el("h1", { id: "auth-h" }, title), ...children);
}

// Wire a form: disable while submitting, show the server's message on failure.
function onSubmit(form, area, handler) {
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    show(area, "");
    try {
      await handler(new FormData(form));
    } catch (err) {
      show(area, err.detail || err.message, "error");
    } finally {
      button.disabled = false;
    }
  });
}

function resendButton(email, area) {
  return el("button", {
    type: "button", class: "btn btn-quiet",
    onclick: async () => {
      try {
        await auth.resend(email);
        show(area, "If the address awaits confirmation, a new link is on its way.");
      } catch (err) {
        show(area, err.detail, "error");
      }
    },
  }, "Resend confirmation email");
}

export function loginView() {
  const flash = auth.peekFlash();
  const area = el("div", { class: "msgs" });
  if (flash) show(area, flash);
  const form = el("form", { class: "stack", novalidate: true },
    field("Email", { name: "email", type: "email", autocomplete: "email", required: true }),
    field("Password", {
      name: "password", type: "password", autocomplete: "current-password", required: true,
    }),
    el("button", { type: "submit", class: "btn" }, "Log in"));
  onSubmit(form, area, async (data) => {
    const email = data.get("email");
    try {
      await auth.login(email, data.get("password"));
    } catch (err) {
      if (err.status === 403) {
        area.replaceChildren(message(err.detail, "error"), resendButton(email, area));
        return;
      }
      throw err;
    }
    navigate("#/overview");
  });
  return card("Log in", area, form,
    el("p", { class: "links" },
      el("a", { href: "#/signup" }, "Create an account"), " · ",
      el("a", { href: "#/reset" }, "Forgot password?")));
}

function timezoneSelect() {
  const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const zones = new Set(Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : []);
  zones.add("UTC");
  zones.add(browserTz);
  const select = el("select", { id: "tz", name: "timezone" },
    [...zones].sort().map((z) => el("option", { value: z, selected: z === browserTz }, z)));
  select.value = browserTz;
  return el("div", { class: "field" }, el("label", { for: "tz" }, "Timezone"), select);
}

export function signupView() {
  const area = el("div", { class: "msgs" });
  const form = el("form", { class: "stack", novalidate: true },
    field("Email", { name: "email", type: "email", autocomplete: "email", required: true }),
    field("Password", {
      name: "password", type: "password", autocomplete: "new-password", minlength: 10,
      required: true,
    }),
    el("p", { class: "hint" }, "At least 10 characters."),
    field("Household name", { name: "household", autocomplete: "off", required: true }),
    timezoneSelect(),
    el("button", { type: "submit", class: "btn" }, "Create account"));
  const view = card("Create your household", area, form,
    el("p", { class: "links" }, "Have an account? ", el("a", { href: "#/login" }, "Log in")));
  onSubmit(form, area, async (data) => {
    const email = data.get("email");
    await auth.signup({
      email,
      password: data.get("password"),
      householdName: data.get("household"),
      timezone: data.get("timezone"),
    });
    checkEmail(view, email);
  });
  return view;
}

function checkEmail(view, email) {
  const notice = el("div", { class: "msgs" });
  view.replaceChildren(
    el("h1", { id: "auth-h" }, "Check your email"),
    el("p", {}, "We sent a confirmation link to ", el("strong", {}, email),
      ". It is valid for 24 hours."),
    notice, resendButton(email, notice),
    el("p", { class: "links" }, el("a", { href: "#/login" }, "Back to log in")));
}

// #/join?invite=…: the invite link from an owner. The token leaves the address bar at once
// and is kept in memory for this page only.
let pendingInvite = null;

export function joinView(params) {
  const fromUrl = params.get("invite");
  if (fromUrl) {
    pendingInvite = fromUrl;
    history.replaceState(null, "", location.pathname + location.search + "#/join");
  }
  const invite = pendingInvite;
  if (!invite) {
    return card("Join a household",
      el("p", {}, "Open the invite link you were sent. Each link works once, for 7 days."),
      el("p", { class: "links" }, el("a", { href: "#/login" }, "Log in"), " · ",
        el("a", { href: "#/signup" }, "Create your own household")));
  }
  const area = el("div", { class: "msgs" });
  const form = el("form", { class: "stack", novalidate: true },
    field("Email", { name: "email", type: "email", autocomplete: "email", required: true }),
    field("Password", {
      name: "password", type: "password", autocomplete: "new-password", minlength: 10,
      required: true,
    }),
    el("p", { class: "hint" }, "At least 10 characters."),
    el("button", { type: "submit", class: "btn" }, "Join"));
  const view = card("Join a household", el("p", {}, "You were invited to a household. Create " +
    "your account to get its alerts; you choose your own sounds and channels."), area, form,
  el("p", { class: "links" }, "Have an account? ", el("a", { href: "#/login" }, "Log in")));
  onSubmit(form, area, async (data) => {
    const email = data.get("email");
    await auth.signup({ email, password: data.get("password"), inviteToken: invite });
    pendingInvite = null;
    checkEmail(view, email);
  });
  return view;
}

// Take a one-time token out of the address bar and history (a reload must not reuse it).
function takeToken(params, hash) {
  const value = params.get("token");
  if (value) history.replaceState(null, "", location.pathname + location.search + hash);
  return value;
}

const confirmations = new Map(); // token -> the single POST for it, whatever re-renders

export async function confirmView(params) {
  const tokenValue = takeToken(params, "#/confirm");
  if (!tokenValue) {
    return card("Confirm your email",
      el("p", {}, "Open the link from your confirmation email, or log in if you already did."),
      el("a", { class: "btn", href: "#/login" }, "Log in"));
  }
  if (!confirmations.has(tokenValue)) confirmations.set(tokenValue, auth.confirm(tokenValue));
  try {
    await confirmations.get(tokenValue);
  } catch (err) {
    return card("Confirm your email", message(err.detail, "error"),
      el("p", { class: "links" }, "Log in to get a fresh link: ",
        el("a", { href: "#/login" }, "Log in")));
  }
  return card("Email confirmed",
    el("p", {}, "Your email is confirmed. You can log in now."),
    el("a", { class: "btn", href: "#/login" }, "Log in"));
}

export function resetView(params) {
  const tokenValue = takeToken(params, "#/reset");
  const area = el("div", { class: "msgs" });
  if (!tokenValue) {
    const form = el("form", { class: "stack", novalidate: true },
      field("Email", { name: "email", type: "email", autocomplete: "email", required: true }),
      el("button", { type: "submit", class: "btn" }, "Send reset link"));
    onSubmit(form, area, async (data) => {
      const { detail } = await auth.requestReset(data.get("email"));
      show(area, detail.charAt(0).toUpperCase() + detail.slice(1) + ".");
    });
    return card("Reset your password", area, form,
      el("p", { class: "links" }, el("a", { href: "#/login" }, "Back to log in")));
  }
  const form = el("form", { class: "stack", novalidate: true },
    field("New password", {
      name: "password", type: "password", autocomplete: "new-password", minlength: 10,
      required: true,
    }),
    el("button", { type: "submit", class: "btn" }, "Set password"));
  onSubmit(form, area, async (data) => {
    await auth.resetPassword(tokenValue, data.get("password"));
    form.remove();
    show(area, "Password changed. Every session was signed out. If someone else had access " +
      "to your account, regenerate your ntfy topic and check your Telegram link in Alert " +
      "channels.");
  });
  return card("Choose a new password", area, form,
    el("p", { class: "links" }, el("a", { href: "#/login" }, "Log in")));
}

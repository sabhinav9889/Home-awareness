// DOM helpers. Server-provided strings only ever become text nodes: never innerHTML.

export const DISCLAIMER =
  "Home-awareness tool. Not a medical device, security alarm, or emergency service.";

// el("button", {type: "button", class: "btn", onclick: fn}, "Label", childNode, ...)
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "class") {
      node.className = value;
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function footer() {
  return el("p", { class: "disclaimer" }, DISCLAIMER);
}

// A labelled input; the id ties <label for> to the control.
let fieldSeq = 0;
export function field(label, attrs = {}) {
  const id = `f${++fieldSeq}`;
  return el("div", { class: "field" },
    el("label", { for: id }, label),
    el("input", { id, ...attrs }));
}

// Messages: role="alert" for errors, role="status" for information.
export function message(text, kind = "status") {
  return el("p", { class: `msg msg-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
}

// Replace an area's message (errors and notices from form submits).
export function show(area, text, kind = "status") {
  area.replaceChildren(text ? message(text, kind) : "");
}

// An accessible confirm dialog (<dialog>, modal): resolves true on confirm, false otherwise.
// `requireText`: the confirm button stays disabled until that exact text is typed into a
// field labelled `requireLabel`. Focus starts on Cancel (or the field), never on the
// destructive button.
// If the alert screen takes over (it makes #root inert), the dialog closes as "cancel" so
// it can never sit on top of an alert.
let dialogSeq = 0;
export function confirmDialog({
  title, body, confirmLabel, danger = false, requireText = null, requireLabel = "Type to confirm",
}) {
  return new Promise((resolve) => {
    const id = `dlg${++dialogSeq}`;
    const ok = el("button", { type: "submit", class: `btn${danger ? " btn-danger" : ""}`,
      value: "ok", disabled: requireText !== null }, confirmLabel);
    const cancel = el("button", { type: "submit", class: "btn btn-quiet", value: "cancel" },
      "Cancel");
    let typed = null;
    if (requireText !== null) {
      typed = el("input", { id: `${id}-in`, autocomplete: "off", spellcheck: "false" });
      typed.addEventListener("input", () => { ok.disabled = typed.value !== requireText; });
    }
    const form = el("form", { method: "dialog", class: "stack" },
      el("h2", { id: `${id}-h` }, title),
      body ? el("p", { id: `${id}-d`, class: "dialog-body" }, body) : null,
      typed ? el("div", { class: "field" },
        el("label", { for: typed.id }, requireLabel), typed) : null,
      el("div", { class: "dialog-actions" }, cancel, ok));
    const dialog = el("dialog", { class: "dialog", "aria-labelledby": `${id}-h`,
      "aria-describedby": body ? `${id}-d` : null }, form);
    const root = document.getElementById("root");
    const watch = new MutationObserver(() => { if (root.inert) dialog.close("cancel"); });
    dialog.addEventListener("close", () => {
      watch.disconnect();
      dialog.remove();
      resolve(dialog.returnValue === "ok" && (requireText === null || typed.value === requireText));
    });
    document.body.append(dialog);
    if (root) watch.observe(root, { attributes: true, attributeFilter: ["inert"] });
    dialog.returnValue = "";
    dialog.showModal();
    (typed || cancel).focus();
  });
}

// Copy text; falls back to selecting `input` (if given) so the person can press Ctrl+C.
export async function copyText(text, input = null) {
  try {
    await navigator.clipboard.writeText(text);
    return "Copied.";
  } catch {
    if (!input) return "Couldn't copy automatically: select it and copy it by hand.";
    input.focus();
    input.select();
    return "Couldn't copy automatically: it's selected, press Ctrl+C (or long-press) to copy.";
  }
}

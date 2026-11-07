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

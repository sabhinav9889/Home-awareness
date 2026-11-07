// Entry point: registers routes and renders each view into the right layout.
import { request } from "./api.js";
import * as auth from "./auth.js";
import { route, start } from "./router.js";
import { el, footer, message } from "./ui.js";
import { confirmView, loginView, resetView, signupView } from "./views/auth.js";
import { overviewView } from "./views/overview.js";
import { soundsView } from "./views/sounds.js";

// Sidebar entries; later views add theirs here.
const NAV = [
  { hash: "#/overview", label: "Overview" },
  { hash: "#/sounds", label: "Sounds & rules" },
];

route("#/login", loginView, { public: true });
route("#/signup", signupView, { public: true });
route("#/confirm", confirmView, { public: true });
route("#/reset", resetView, { public: true });
route("#/overview", overviewView);
route("#/sounds", soundsView);

const root = document.getElementById("root");
let renderSeq = 0;

function publicLayout(view) {
  return el("div", { class: "auth-layout" },
    el("div", { class: "brand" }, logo(), el("span", {}, "Home Awareness")),
    el("main", { id: "main" }, view, footer()));
}

function logo() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  for (const [k, v] of Object.entries({
    width: 28, height: 28, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", class: "logo",
  })) svg.setAttribute(k, v);
  for (const d of ["M3 11l9-7 9 7", "M5 10v10h14V10", "M10 15a3 3 0 0 1 4 0",
    "M8.5 13a6 6 0 0 1 7 0"]) {
    const p = document.createElementNS(ns, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

function appLayout(view, path, household) {
  return el("div", { class: "app-layout" },
    el("nav", { class: "sidebar", "aria-label": "Main" },
      el("div", { class: "brand" }, logo(), el("span", {}, "Home Awareness")),
      el("div", { class: "nav-links" },
        NAV.map((n) => el("a", {
          href: n.hash, class: "nav-link", "aria-current": n.hash === path ? "page" : null,
        }, n.label))),
      el("div", { class: "nav-spacer" }),
      el("div", { class: "household-box" },
        el("div", { class: "household-name" }, household.name),
        el("button", { type: "button", class: "btn btn-quiet", onclick: () => auth.logout() },
          "Log out"))),
    el("main", { id: "main", class: "content" }, view, footer()));
}

async function renderRoute(entry, params, path) {
  const seq = ++renderSeq;
  try {
    if (entry.isPublic) {
      const view = await entry.render(params);
      if (seq === renderSeq) root.replaceChildren(publicLayout(view));
      return;
    }
    const household = await request("GET", "/households/me");
    const result = await entry.render(params, { household });
    const { node, cleanup } = result instanceof Node ? { node: result } : result;
    if (seq === renderSeq) root.replaceChildren(appLayout(node, path, household));
    return cleanup; // the router runs it on leaving (or at once if this render is stale)
  } catch (err) {
    if (seq !== renderSeq || err.status === 401) return; // a 401 already went to login
    root.replaceChildren(publicLayout(el("section", { class: "card auth-card" },
      message(err.detail || err.message, "error"))));
  }
}

start(renderRoute, "#/overview");

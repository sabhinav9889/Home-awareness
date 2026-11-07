// Hash router: "#/path?query". Views render into the shell; private routes need a session.
import { token } from "./auth.js";

const routes = new Map();
let onRender = () => {};
let fallback = "#/overview";

// route("#/login", render, {public: true});
// render(params: URLSearchParams, ctx) -> Node | {node, cleanup} (or a Promise of either);
// `cleanup()` runs when the user leaves the route.
// `guestOnly`: with a session, the route redirects to the default route instead.
export function route(hash, renderFn, { public: isPublic = false, guestOnly = false } = {}) {
  routes.set(hash, { render: renderFn, isPublic, guestOnly });
}

export function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

export function start(renderInto, defaultHash = fallback) {
  onRender = renderInto;
  fallback = defaultHash;
  window.addEventListener("hashchange", render);
  render();
}

// The renderer may resolve to a cleanup function (a view's sockets, timers): it runs before
// the next route renders. A cleanup that arrives after a newer render started runs at once.
let cleanup = null;
let seq = 0;

function runCleanup() {
  const fn = cleanup;
  cleanup = null;
  if (fn) fn();
}

function render() {
  runCleanup();
  const mine = ++seq;
  const [path, query = ""] = (location.hash || "#/").split("?");
  const entry = routes.get(path);
  if (!entry) return navigate(token() ? fallback : "#/login");
  if (!entry.isPublic && !token()) return navigate("#/login");
  if (entry.guestOnly && token()) return navigate(fallback);
  Promise.resolve(onRender(entry, new URLSearchParams(query), path)).then((fn) => {
    if (typeof fn !== "function") return;
    if (mine === seq) cleanup = fn;
    else fn();
  });
}

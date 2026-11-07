// Hash router: "#/path?query". Views render into the shell; private routes need a session.
import { token } from "./auth.js";

const routes = new Map();
let onRender = () => {};
let fallback = "#/overview";

// route("#/login", render, {public: true}); render(params: URLSearchParams) -> Node | Promise<Node>
export function route(hash, renderFn, { public: isPublic = false } = {}) {
  routes.set(hash, { render: renderFn, isPublic });
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

function render() {
  const [path, query = ""] = (location.hash || "#/").split("?");
  const entry = routes.get(path);
  if (!entry) return navigate(token() ? fallback : "#/login");
  if (!entry.isPublic && !token()) return navigate("#/login");
  onRender(entry, new URLSearchParams(query), path);
}

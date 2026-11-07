// Placeholder overview (Task 2 fills it in).
import { el } from "../ui.js";

export function overviewView(_params, { household }) {
  return el("section", { class: "stack" },
    el("header", { class: "page-head" }, el("h1", {}, "Overview")),
    el("p", {}, "Welcome to ", el("strong", {}, household.name), "."));
}

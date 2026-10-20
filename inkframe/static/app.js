// Inkframe web UI: the shell, a tiny hash router and the connection badge.
//
// Each view module exports mount(container, ...params) and returns
// {destroy, isDirty}; the router calls destroy() before mounting the next
// view (timers, listeners, polling stop there) and asks isDirty() before
// leaving a page with unsaved edits.

import { api, onConnection } from "./js/api.js";
import { appsView } from "./js/apps.js";
import { editorView } from "./js/editor.js";
import { nowView } from "./js/now.js";
import { photosView } from "./js/photos.js";
import { rotationView } from "./js/rotation.js";
import { settingsView } from "./js/settings.js";
import { confirmDialog, icon } from "./js/ui.js";

const ROUTES = [
  { re: /^\/now$/, tab: "now", page: "now", view: nowView },
  { re: /^\/photos$/, tab: "photos", page: "photos", view: photosView },
  { re: /^\/photos\/([0-9a-f]{32})$/, tab: "photos", page: "editor", view: editorView },
  { re: /^\/rotation$/, tab: "rotation", page: "rotation", view: rotationView },
  { re: /^\/apps$/, tab: "apps", page: "apps", view: appsView },
  { re: /^\/settings$/, tab: "settings", page: "settings", view: settingsView },
];

const main = document.getElementById("view");
let current = null; // {destroy, isDirty} of the mounted view
let currentHash = "";
let leaving = false; // set while a confirmed "discard changes" navigation runs

function pathOf(hash) {
  return hash.replace(/^#/, "") || "/now";
}

function setActiveTab(tab) {
  for (const a of document.querySelectorAll(".nav-list a")) {
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

function route() {
  const path = pathOf(location.hash);
  let match = null;
  let found = null;
  for (const r of ROUTES) {
    match = path.match(r.re);
    if (match) {
      found = r;
      break;
    }
  }
  if (!found) {
    location.replace("#/now");
    return;
  }
  if (current && current.destroy) {
    try {
      current.destroy();
    } catch (e) {
      console.error("view cleanup failed", e);
    }
  }
  currentHash = location.hash || "#/now";
  setActiveTab(found.tab);
  main.replaceChildren();
  main.dataset.view = found.tab;
  main.dataset.page = found.page;
  window.scrollTo(0, 0);
  current = found.view(main, ...match.slice(1)) || null;
  // move focus for screen readers without drawing a ring on the container
  main.focus({ preventScroll: true });
}

window.addEventListener("hashchange", async () => {
  if (!leaving && current && current.isDirty && current.isDirty()) {
    const target = location.hash;
    // stay on the page while asking (replaceState fires no hashchange)
    history.replaceState(null, "", currentHash);
    const ok = await confirmDialog({
      title: "Discard your changes?",
      message: "This page has edits that are not saved yet.",
      confirm: "Discard",
      cancel: "Keep editing",
      danger: true,
    });
    if (ok) {
      leaving = true;
      location.hash = target;
    }
    return;
  }
  leaving = false;
  route();
});

window.addEventListener("beforeunload", (e) => {
  if (current && current.isDirty && current.isDirty()) {
    e.preventDefault();
    e.returnValue = "";
  }
});

// --- shell ------------------------------------------------------------------------------

for (const slot of document.querySelectorAll("[data-icon]")) {
  slot.replaceWith(icon(slot.dataset.icon, "nav-icon"));
}

onConnection((online) => {
  for (const el of document.querySelectorAll("[data-conn]")) {
    el.dataset.state = online ? "online" : "offline";
    el.querySelector(".conn-label").textContent = online ? "Online" : "Offline";
  }
});

// one quiet status read for the host name in the rail and the connection badge
api("/api/status", { quiet: true })
  .then((status) => {
    const host = document.querySelector("[data-host]");
    if (host && status && status.system) host.textContent = `${status.system.hostname} · v${status.version}`;
  })
  .catch(() => {});

route();

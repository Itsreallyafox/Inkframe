// DOM helpers, icons, toasts, the confirm dialog and formatting.
// Everything user-supplied (photo names, app settings, log lines) goes in as
// text nodes; only the static icon markup below is ever parsed as HTML.

// Element properties that must be set as properties, not attributes.
const PROPS = new Set(["value", "checked", "selected", "indeterminate", "disabled", "hidden"]);

/**
 * h("button", {class: "btn", onclick: fn}, "Save") -> <button class="btn">Save</button>
 * Attributes with null/false values are skipped; children may be strings,
 * nodes, arrays or null.
 */
export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "dataset") Object.assign(el.dataset, value);
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2), value);
      } else if (PROPS.has(key)) el[key] = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  append(el, children);
  return el;
}

export function append(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Replace an element's children; null/false children are skipped (unlike replaceChildren). */
export function setChildren(el, ...children) {
  el.replaceChildren();
  return append(el, children);
}

// --- icons (24 px grid, stroked with currentColor) ---------------------------

const ICONS = {
  now: '<rect x="3" y="4.5" width="18" height="13" rx="1.5"/><path d="M8.5 21h7M12 17.5V21"/>',
  photos: '<rect x="3" y="4" width="18" height="16" rx="1.5"/><circle cx="9" cy="9.5" r="1.8"/><path d="m21 15.5-4.8-4.8L7 20"/>',
  rotation: '<path d="M19.5 13a7.5 7.5 0 0 1-13.4 3.6"/><path d="M4.5 11a7.5 7.5 0 0 1 13.4-3.6"/><path d="M18.5 3.5v4h-4"/><path d="M5.5 20.5v-4h4"/>',
  apps: '<rect x="4" y="4" width="6.5" height="6.5" rx="1"/><rect x="13.5" y="4" width="6.5" height="6.5" rx="1"/><rect x="4" y="13.5" width="6.5" height="6.5" rx="1"/><rect x="13.5" y="13.5" width="6.5" height="6.5" rx="1"/>',
  settings: '<path d="M4 7h9.5M18.5 7H20M4 17h3.5M12.5 17H20"/><circle cx="16" cy="7" r="2.3"/><circle cx="10" cy="17" r="2.3"/>',
  show: '<rect x="3" y="4.5" width="18" height="13" rx="1.5"/><path d="M10 8.5v5l4.2-2.5z"/><path d="M8.5 21h7"/>',
  eye: '<path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.6"/>',
  eyeOff: '<path d="M2.5 12S6 6 12 6s9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6z"/><circle cx="12" cy="12" r="2.6"/><path d="M4 4l16 16"/>',
  trash: '<path d="M4.5 7h15M9.5 7V4.5h5V7M6.5 7l.9 12.5h9.2l.9-12.5"/>',
  grip: '<g fill="currentColor" stroke="none"><circle cx="9" cy="6" r="1.4"/><circle cx="15" cy="6" r="1.4"/><circle cx="9" cy="12" r="1.4"/><circle cx="15" cy="12" r="1.4"/><circle cx="9" cy="18" r="1.4"/><circle cx="15" cy="18" r="1.4"/></g>',
  up: '<path d="m6.5 14.5 5.5-5.5 5.5 5.5"/>',
  down: '<path d="m6.5 9.5 5.5 5.5 5.5-5.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4.2h-4.2"/>',
  back: '<path d="M19 12H5.5M11 18l-6-6 6-6"/>',
  next: '<path d="M5.5 5.5 14 12l-8.5 6.5z"/><path d="M18 5.5v13"/>',
  pause: '<path d="M8.5 5.5v13M15.5 5.5v13"/>',
  play: '<path d="M7.5 5.5 18.5 12l-11 6.5z"/>',
  close: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  seed: '<rect x="4" y="4" width="16" height="16" rx="3"/><g fill="currentColor" stroke="none"><circle cx="8.8" cy="8.8" r="1.3"/><circle cx="15.2" cy="15.2" r="1.3"/><circle cx="12" cy="12" r="1.3"/></g>',
  upload: '<path d="M12 15.5V4.5M7.5 9 12 4.5 16.5 9"/><path d="M4.5 15v4.5h15V15"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  alert: '<path d="M12 4 21 19.5H3z"/><path d="M12 10v4.5M12 17v.5"/>',
};

/** An inline SVG icon (decorative: hidden from assistive tech). */
export function icon(name, cls = "") {
  const span = document.createElement("span");
  span.className = `icon ${cls}`.trim();
  span.setAttribute("aria-hidden", "true");
  span.innerHTML =
    '<svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" ' +
    'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">' +
    (ICONS[name] || "") +
    "</svg>";
  return span;
}

/** The six panel colours as a little strip: the app's signature mark. */
export function swatchStrip(cls = "") {
  return h(
    "span",
    { class: `swatches ${cls}`.trim(), "aria-hidden": "true" },
    ["black", "white", "yellow", "red", "blue", "green"].map((c) => h("i", { class: `sw-${c}` })),
  );
}

// --- toasts -----------------------------------------------------------------------

/** Show a toast; kind is "info", "ok", "warn" or "error". Returns a dismiss function. */
export function toast(message, kind = "info", timeout) {
  const region = document.getElementById("toasts");
  if (!region) return () => {};
  const ms = timeout ?? (kind === "error" ? 7000 : 4000);
  const close = h("button", { class: "toast-close", type: "button", "aria-label": "Dismiss" }, icon("close"));
  const el = h("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" },
    h("span", { class: "toast-msg" }, message), close);
  let timer = 0;
  const dismiss = () => {
    clearTimeout(timer);
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 180);
  };
  close.addEventListener("click", dismiss);
  region.append(el);
  // keep at most four on screen
  while (region.children.length > 4) region.firstElementChild.remove();
  if (ms > 0) timer = setTimeout(dismiss, ms);
  return dismiss;
}

// --- confirm dialog ------------------------------------------------------------------

/**
 * Resolves true when the user confirms. Uses the <dialog id="confirm"> in
 * index.html so focus is trapped and Escape cancels.
 */
export function confirmDialog({ title, message, confirm = "OK", cancel = "Cancel", danger = false, image = "" }) {
  const dlg = document.getElementById("confirm");
  if (!dlg || typeof dlg.showModal !== "function") {
    return Promise.resolve(window.confirm(`${title}\n\n${message || ""}`));
  }
  dlg.querySelector("[data-title]").textContent = title;
  dlg.querySelector("[data-message]").textContent = message || "";
  // an optional picture of what is at stake (a photo's render, on delete)
  const thumb = dlg.querySelector("[data-image]");
  if (thumb) {
    thumb.hidden = !image;
    if (image) thumb.src = image;
    else thumb.removeAttribute("src");
  }
  const ok = dlg.querySelector("[data-ok]");
  const no = dlg.querySelector("[data-cancel]");
  ok.textContent = confirm;
  no.textContent = cancel;
  ok.className = danger ? "btn btn-danger-solid" : "btn btn-primary";
  return new Promise((resolve) => {
    const done = (value) => {
      ok.removeEventListener("click", onOk);
      dlg.removeEventListener("close", onClose);
      if (dlg.open) dlg.close();
      resolve(value);
    };
    const onOk = () => done(true);
    const onClose = () => done(false);
    ok.addEventListener("click", onOk);
    dlg.addEventListener("close", onClose);
    no.onclick = () => done(false);
    dlg.showModal();
    // the safe choice gets the focus, so Enter on a stray keypress cancels
    no.focus();
  });
}

// --- timing ---------------------------------------------------------------------------

/** Trailing-edge debounce with .cancel() and .flush(). */
export function debounce(fn, ms) {
  let timer = 0;
  let lastArgs = null;
  const run = () => {
    timer = 0;
    const args = lastArgs;
    lastArgs = null;
    fn(...args);
  };
  const debounced = (...args) => {
    lastArgs = args;
    clearTimeout(timer);
    timer = setTimeout(run, ms);
  };
  debounced.cancel = () => {
    clearTimeout(timer);
    timer = 0;
    lastArgs = null;
  };
  debounced.flush = () => {
    if (timer) {
      clearTimeout(timer);
      run();
    }
  };
  debounced.pending = () => timer !== 0;
  return debounced;
}

// --- formatting -------------------------------------------------------------------------

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;
// a number and its unit never break apart: "9:47 PM", "4 min", "12 MB"
const NB = "\u00a0";
const glue = (text) => text.replace(/(\d)\s+(?=[A-Za-z%])/g, `$1${NB}`);

export function parseTime(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isNaN(t) ? null : t;
}

function timeOf(d) {
  return glue(d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }));
}

/** "" for today, else "tomorrow", "yesterday" or "21 Oct". */
function dayOf(d, now) {
  if (d.toDateString() === new Date(now).toDateString()) return "";
  if (d.toDateString() === new Date(now + DAY).toDateString()) return "tomorrow";
  if (d.toDateString() === new Date(now - DAY).toDateString()) return "yesterday";
  return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }).replace(/\s+/g, NB);
}

/** "9:46 PM" (the device's clock style), with the day when it is not today. */
export function fmtClock(iso, now = Date.now()) {
  const t = parseTime(iso);
  if (t === null) return "never";
  const d = new Date(t);
  const day = dayOf(d, now);
  return day ? `${day} ${timeOf(d)}` : timeOf(d);
}

/** "at 9:46 PM", "tomorrow at 7:00 AM", "21 Oct at 7:00 AM". */
export function fmtAt(iso, now = Date.now()) {
  const t = parseTime(iso);
  if (t === null) return "";
  const d = new Date(t);
  const day = dayOf(d, now);
  return day ? `${day} at ${timeOf(d)}` : `at ${timeOf(d)}`;
}

/** A settings time such as "23:00" in the same clock style ("11:00 PM"). */
export function fmtHM(hm) {
  const m = String(hm || "").match(/^(\d{1,2}):(\d{2})/);
  if (!m) return String(hm || "");
  return timeOf(new Date(2000, 0, 1, Number(m[1]), Number(m[2])));
}

export function fmtDate(iso) {
  const t = parseTime(iso);
  if (t === null) return "";
  return new Date(t).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

/** "just now", "4 min ago", "3 h ago", "2 days ago". */
export function fmtAgo(iso, now = Date.now()) {
  const t = parseTime(iso);
  if (t === null) return "";
  const ms = now - t;
  if (ms < 45_000) return "just now";
  if (ms < HOUR) return `${Math.round(ms / MIN)}${NB}min ago`;
  if (ms < DAY) return `${Math.floor(ms / HOUR)}${NB}h ago`;
  const days = Math.floor(ms / DAY);
  return days === 1 ? "yesterday" : `${days}${NB}days ago`;
}

/** A countdown: "0:42", "12:05", "1 h 04 min", "2 d 3 h". */
export function fmtCountdown(ms) {
  const s = Math.max(0, Math.ceil(ms / 1000));
  if (s < 3600) {
    const m = Math.floor(s / 60);
    return `${m}:${String(s % 60).padStart(2, "0")}`;
  }
  if (s < 86400) {
    const hrs = Math.floor(s / 3600);
    return `${hrs}${NB}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}${NB}min`;
  }
  return `${Math.floor(s / 86400)}${NB}d ${Math.floor((s % 86400) / 3600)}${NB}h`;
}

/** Uptime-style duration: "3 d 2 h", "5 h 12 min", "4 min". */
export function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const hrs = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}${NB}d ${hrs}${NB}h`;
  if (hrs) return `${hrs}${NB}h ${m}${NB}min`;
  if (m) return `${m}${NB}min`;
  return `${s}${NB}s`;
}

export function fmtBytes(n) {
  if (!Number.isFinite(n)) return "?";
  if (n < 1024) return `${n}${NB}B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)}${NB}${units[i]}`;
}

/** "3 min", "1 h 30 min", "1 day". */
export function fmtMinutes(min) {
  if (min < 60) return `${min}${NB}min`;
  const hrs = Math.floor(min / 60);
  const m = min % 60;
  if (hrs >= 24 && m === 0 && hrs % 24 === 0) return hrs === 24 ? `1${NB}day` : `${hrs / 24}${NB}days`;
  return m ? `${hrs}${NB}h ${m}${NB}min` : `${hrs}${NB}h`;
}

/** A photo's file name without its image extension, for labels and toasts. */
export function displayName(name) {
  const text = String(name || "");
  return text.replace(/\.(jpe?g|png|gif|webp|bmp|tiff?|heic|heif|avif)$/i, "") || text;
}

/**
 * A server message shown under the field it names, without the field's key:
 * "count: must be at least 3, got 0" -> "Must be at least 3, got 0".
 */
export function fieldMessage(message, key) {
  let text = String(message || "").trim();
  if (key && text.toLowerCase().startsWith(String(key).toLowerCase())) {
    const rest = text.slice(String(key).length);
    if (/^(:|\s+(must|is|should|needs)\b)/.test(rest)) text = rest.replace(/^:?\s*/, "");
  }
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** A section heading in the small-caps "eyebrow" style with a hairline. */
export function eyebrow(text, ...extra) {
  return h("div", { class: "eyebrow" }, h("h2", null, text), ...extra);
}

/** A labelled on/off switch (a styled checkbox). */
export function switchInput({ checked, label, onchange, id }) {
  const input = h("input", { type: "checkbox", class: "switch", role: "switch", checked: !!checked, id });
  if (label) input.setAttribute("aria-label", label);
  if (onchange) input.addEventListener("change", () => onchange(input.checked));
  return input;
}

/**
 * A segmented control (radio buttons styled as one bar).
 * options: [{value, label}]; returns {el, set(value)}.
 */
let segCount = 0;
export function segmented({ options, value, onchange, label, small = false }) {
  segCount += 1;
  const name = `seg${segCount}`;
  const el = h("div", { class: `seg${small ? " seg-sm" : ""}`, role: "radiogroup", "aria-label": label || null });
  const inputs = options.map((opt) => {
    const id = `${name}-${opt.value}`;
    const input = h("input", { type: "radio", name, id, value: String(opt.value), checked: String(opt.value) === String(value) });
    input.addEventListener("change", () => {
      if (input.checked && onchange) onchange(opt.value);
    });
    el.append(input, h("label", { for: id, title: opt.title || null }, opt.label));
    return input;
  });
  return {
    el,
    set(v) {
      for (const input of inputs) input.checked = input.value === String(v);
    },
  };
}

/** A skeleton/loading block for first paint. */
export function loading(text = "Loading") {
  return h("div", { class: "loading", role: "status" }, h("span", { class: "loading-bar" }), h("span", null, `${text}…`));
}

/** An empty state: a quiet heading and a line of guidance. */
export function emptyState(title, text, ...actions) {
  return h("div", { class: "empty" }, swatchStrip("swatches-lg"), h("p", { class: "empty-title" }, title), h("p", { class: "empty-text" }, text), actions.length ? h("div", { class: "empty-actions" }, actions) : null);
}

export function pageHead(title, sub, ...actions) {
  return h(
    "header",
    { class: "page-head" },
    h("div", { class: "page-titles" }, h("h1", { class: "page-title" }, title), sub ? h("p", { class: "page-sub" }, sub) : null),
    actions.length ? h("div", { class: "page-actions" }, actions) : null,
  );
}

// The photo editor: pick a dither method and tune the pre-adjustments while
// watching the real panel render.
//
// Preview pipeline.  POST /api/photos/{id}/previews renders one set of knobs
// with the methods it is asked for ("methods": [...], or all twelve) and
// answers with a URL per method; renders are cached by key, so asking again
// is cheap.  Answers are kept per knob signature in `known`.  pump() decides
// what to ask for next, with exactly one request in flight:
//
//   1. the selected method with the current knobs (the large stage), alone,
//      so a slider move costs one dither, not twelve, on the Pi;
//   2. once the knobs have settled for 1.5 s (or on "Refresh all"), the
//      other methods, four per request, so a new slider move never waits
//      behind a long twelve-method render.
//
// Knob changes are debounced 300 ms before pump() runs; an answer that
// arrives for knobs that are no longer current is kept (a later return to
// those knobs is instant) but not shown.  Until the strip has caught up with
// the current knobs its tiles are marked stale rather than swapped one by
// one while a slider is still moving.

import { api, getRecipeMeta, queuedMessage, setRecipeMeta } from "./api.js";
import { confirmDialog, debounce, displayName, emptyState, eyebrow, fmtDate, h, icon, loading, segmented, switchInput, toast } from "./ui.js";

const PREVIEW_DEBOUNCE_MS = 300;
const STRIP_SETTLE_MS = 1500;
const STRIP_CHUNK = 4;
const LONG_PRESS_MS = 450;
const KEEP_SIGS = 12;
// the detail the method tiles show, in panel pixels (3:2 like the tiles)
const DETAIL_W = 72;
const DETAIL_H = 48;

const SLIDERS = [
  { key: "strength", label: "Strength", step: 0.01, neutral: 0.85, help: "How much of each pixel's colour error is passed on" },
  { key: "brightness", label: "Brightness", step: 0.01, neutral: 1 },
  { key: "contrast", label: "Contrast", step: 0.01, neutral: 1 },
  { key: "saturation", label: "Saturation", step: 0.01, neutral: 1 },
  { key: "gamma", label: "Gamma", step: 0.01, neutral: 1, help: "Above 1 lifts the shadows" },
  { key: "sharpen", label: "Sharpen", step: 0.05, neutral: 0 },
];
const KNOB_KEYS = ["strength", "brightness", "contrast", "saturation", "gamma", "sharpen", "serpentine", "distance", "palette"];
const KIND_LABELS = { error_diffusion: "diffusion", ordered: "ordered", nearest: "nearest" };

function pickKnobs(recipe) {
  const out = {};
  for (const k of KNOB_KEYS) out[k] = recipe[k];
  return out;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

export function editorView(root, id) {
  let destroyed = false;
  let photo = null;
  let meta = null;
  let methods = [];
  let knobs = {};
  let method = "";
  let saved = null; // {method, ...knobs} of the stored effective recipe
  const known = new Map(); // knob signature -> Map(method id -> preview)
  let inflight = false;
  let blocked = ""; // a signature whose request failed: not retried by itself
  let stripWanted = true; // the strip should catch up with the current knobs
  let stageLoading = false;
  let stageUrl = "";
  let busySaving = false;
  let followers = null; // photos without their own recipe (they follow the default)
  let actionsInView = false;
  let focus = { x: 300, y: 200 }; // centre of the detail, in panel pixels
  const observers = [];

  root.append(h("a", { class: "back", href: "#/photos" }, icon("back"), "Photos"), loading("Opening the photo"));

  const sig = () => JSON.stringify(knobs);
  const isDirty = () => !!saved && (method !== saved.method || KNOB_KEYS.some((k) => knobs[k] !== saved[k]));
  const methodName = (mid) => (methods.find((m) => m.id === mid) || {}).name || mid;

  // --- elements (built once the photo has loaded) ---------------------------------------
  let el = {};

  function build() {
    const title = h("h1", { class: "page-title editor-title", title: photo.original_name }, displayName(photo.original_name));
    const metaLine = h("p", { class: "page-sub mono" });
    const head = h("header", { class: "page-head" }, h("div", { class: "page-titles" }, title, metaLine));

    // the stage: sticky, so the render stays in view while the strip or the
    // sliders below it are scrolled
    const zoom = segmented({
      label: "Zoom",
      small: true,
      value: "fit",
      options: [
        { value: "fit", label: "Fit", title: "Fit the preview to the page" },
        { value: "1", label: "1:1", title: "Actual size: the panel's 600 × 400 pixels" },
        { value: "2", label: "2×", title: "Twice the actual size: the dither pattern up close" },
      ],
      onchange: setZoom,
    });
    const capName = h("span", { class: "stage-cap-name" });
    const took = h("span", { class: "took" });
    const stageImg = h("img", { class: "stage-img", width: 600, height: 400, alt: "", draggable: "false" });
    const focusBox = h("span", { class: "stage-focus", "aria-hidden": "true" });
    const stageScroll = h("div", { class: "stage-scroll", dataset: { zoom: "fit" } }, stageImg, focusBox);
    const busy = h("div", { class: "stage-busy", "aria-hidden": "true" }, h("span", { class: "refresh-bar" }, ["black", "white", "yellow", "red", "blue", "green"].map((c) => h("i", { class: `sw-${c}` }))), h("span", { class: "stage-busy-label mono" }, "Rendering"));
    const bezel = h("div", { class: "bezel stage-bezel" }, h("div", { class: "screen" }, stageScroll), busy);
    const stage = h("section", { class: "editor-stage", "aria-label": "Preview" }, bezel,
      h("div", { class: "stage-bar" }, h("p", { class: "stage-cap mono", "aria-live": "polite" }, capName, took), zoom.el));
    const sentinel = h("div", { class: "stage-sentinel", "aria-hidden": "true" });

    // the method strip
    const view = segmented({
      label: "Tiles show",
      small: true,
      value: "detail",
      options: [
        { value: "detail", label: "Detail", title: "Each tile shows the same small detail at twice its size (D)" },
        { value: "whole", label: "Whole", title: "Each tile shows the whole picture (D)" },
      ],
      onchange: setStripView,
    });
    const refreshAll = h("button", { class: "btn btn-sm", type: "button", onclick: () => refreshStrip(), title: "Render every method with the current adjustments (R)" }, icon("refresh"), h("span", null, "Refresh"));
    const strip = h("div", { class: "strip", role: "listbox", "aria-label": "Dither method", "aria-orientation": "horizontal", dataset: { view: "detail" } });
    const tip = h("div", { class: "tip", role: "tooltip", hidden: true });
    const tiles = new Map();
    for (const m of methods) {
      const img = h("img", { alt: "", width: 600, height: 400, decoding: "async", draggable: "false" });
      const tileEl = h("button", { class: "mtile", type: "button", role: "option", "aria-selected": "false", tabindex: "-1", title: m.description, dataset: { method: m.id } },
        h("span", { class: "mtile-img" }, img),
        h("span", { class: "mtile-text" }, h("span", { class: "mtile-name" }, m.name), h("span", { class: "mtile-kind mono" }, KIND_LABELS[m.kind] || m.kind)),
        h("span", { class: "mtile-saved", title: "The method saved for this photo" }, "saved"));
      img.addEventListener("load", () => tileEl.classList.add("loaded"));
      wireTile(tileEl, m, tip);
      tiles.set(m.id, { el: tileEl, img, sig: "" });
      strip.append(tileEl);
    }
    strip.addEventListener("keydown", onStripKey);
    const stripNote = h("span", { class: "strip-note mono", hidden: true }, "updating");
    const desc = h("p", { class: "method-desc" });
    const hint = h("p", { class: "section-hint" },
      h("span", { class: "only-touch" }, "Every method with the current adjustments. Tap one to see it large; tap the preview to choose the detail."),
      h("span", { class: "only-pointer" }, "Every method with the current adjustments. Click one (or press ← →) to see it large; click the preview to choose the detail."));
    const methodsSection = h("section", { class: "editor-methods" }, eyebrow("Methods", stripNote, view.el, refreshAll), hint, h("div", { class: "strip-wrap" }, strip, tip), desc);

    // adjustments
    const sliders = new Map();
    const sliderRows = SLIDERS.map((s) => {
      const lim = meta.limits[s.key] || { min: 0, max: 2 };
      const input = h("input", { type: "range", min: lim.min, max: lim.max, step: s.step, value: knobs[s.key], id: `knob-${s.key}`, "aria-describedby": s.help ? `knob-${s.key}-help` : null });
      const out = h("output", { class: "knob-value mono", for: `knob-${s.key}` }, Number(knobs[s.key]).toFixed(2));
      input.addEventListener("input", () => {
        knobs[s.key] = Number(input.value);
        out.textContent = Number(input.value).toFixed(2);
        knobsChanged();
      });
      // double-click (or double-tap) the value to go back to neutral
      out.addEventListener("dblclick", () => {
        input.value = String(s.neutral);
        input.dispatchEvent(new Event("input"));
      });
      out.title = `Double-click for ${s.neutral.toFixed(2)}`;
      sliders.set(s.key, { input, out });
      return h("div", { class: `knob${s.help ? " has-help" : ""}`, dataset: { knob: s.key } },
        h("div", { class: "knob-head" }, h("label", { for: `knob-${s.key}` }, s.label), out),
        input,
        s.help ? h("p", { class: "knob-help", id: `knob-${s.key}-help` }, s.help) : null);
    });

    const serp = switchInput({ checked: knobs.serpentine, label: "Serpentine scan", id: "knob-serpentine", onchange: (v) => { knobs.serpentine = v; knobsChanged(); } });
    const dist = segmented({ label: "Colour distance", small: true, value: knobs.distance, options: (meta.limits.distance?.choices || ["rgb", "lab"]).map((c) => ({ value: c, label: c === "lab" ? "Lab" : c.toUpperCase() })), onchange: (v) => { knobs.distance = v; knobsChanged(); } });
    const pal = segmented({ label: "Palette", small: true, value: knobs.palette, options: (meta.limits.palette?.choices || ["nominal", "measured"]).map((c) => ({ value: c, label: c[0].toUpperCase() + c.slice(1) })), onchange: (v) => { knobs.palette = v; knobsChanged(); } });
    const toggles = h("div", { class: "toggles" },
      h("div", { class: "toggle-row", dataset: { knob: "serpentine" } }, h("label", { for: "knob-serpentine" }, "Serpentine", h("span", { class: "toggle-help" }, "Alternate the scan direction per row (error diffusion)")), serp),
      h("div", { class: "toggle-row" }, h("span", { class: "toggle-label" }, "Colour distance", h("span", { class: "toggle-help" }, "Lab matches colours as the eye sees them")), dist.el),
      h("div", { class: "toggle-row" }, h("span", { class: "toggle-label" }, "Palette", h("span", { class: "toggle-help" }, "Measured: the colours the panel really shows")), pal.el));

    const status = h("p", { class: "editor-status", role: "status" });
    const saveBtn = h("button", { class: "btn btn-primary", type: "button", onclick: save, title: "Save this recipe for the photo (Ctrl+S)" }, icon("check"), h("span", null, "Save"));
    const showBtn = h("button", { class: "btn", type: "button", onclick: showOnPanel }, icon("show"), h("span", null, "Show on panel"));
    const defaultBtn = h("button", { class: "btn-link", type: "button", onclick: saveAsDefault }, "Make this the default…");
    const defaultNote = h("p", { class: "field-help default-note" });
    const resetBtn = h("button", { class: "btn-link", type: "button", onclick: reset }, "Reset to the default recipe");
    const actions = h("div", { class: "editor-actions" },
      status,
      h("div", { class: "editor-actions-main" }, saveBtn, showBtn),
      h("div", { class: "editor-actions-more" }, h("div", null, defaultBtn, defaultNote), resetBtn));
    const keys = h("p", { class: "kbd-hint" }, h("kbd", null, "←"), h("kbd", null, "→"), " method · ", h("kbd", null, "Z"), " zoom · ", h("kbd", null, "D"), " detail · ", h("kbd", null, "R"), " refresh · ", h("kbd", null, "Ctrl S"), " save");

    const panel = h("aside", { class: "editor-panel", "aria-label": "Adjustments" }, eyebrow("Adjustments"), h("div", { class: "knobs" }, sliderRows), toggles, actions, keys);

    // phones: the actions sit at the end of a long page, so unsaved edits
    // also raise a bar above the tab bar, hidden while the actions
    // themselves are on screen (and on wide screens, where the sticky panel
    // keeps Save in view)
    const barSave = h("button", { class: "btn btn-primary", type: "button", onclick: save }, icon("check"), h("span", null, "Save"));
    const savebar = h("div", { class: "savebar editor-savebar", hidden: true }, h("p", { class: "savebar-msg" }, "Unsaved changes to this photo"), h("div", { class: "savebar-actions" }, barSave));

    root.replaceChildren(h("a", { class: "back", href: "#/photos" }, icon("back"), "Photos"), head,
      h("div", { class: "editor" }, h("div", { class: "editor-main" }, sentinel, stage, methodsSection), panel), savebar);
    el = { title, metaLine, zoom, view, capName, took, stage, stageImg, stageScroll, focusBox, bezel, desc, strip, stripNote, tiles, tip, sliders, serp, dist, pal, status, saveBtn, showBtn, defaultBtn, defaultNote, resetBtn, toggles, savebar, barSave };

    wireStage();
    if ("IntersectionObserver" in window) {
      const io = new IntersectionObserver((entries) => {
        actionsInView = entries.some((e) => e.isIntersecting);
        renderDirty();
      });
      io.observe(actions);
      // a hairline under the stage once it is stuck below the nav
      const navH = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--nav-h")) || 0;
      const stuck = new IntersectionObserver(([e]) => {
        stage.classList.toggle("stuck", !e.isIntersecting && e.boundingClientRect.top < (e.rootBounds ? e.rootBounds.top : navH) + 1);
      }, { rootMargin: `-${navH + 1}px 0px 0px 0px` });
      stuck.observe(sentinel);
      observers.push(io, stuck);
    }
    setFocus(focus);
  }

  // --- rendering ------------------------------------------------------------------------

  function renderMeta() {
    const parts = [`${photo.width} × ${photo.height}`, `added ${fmtDate(photo.created_at)}`, photo.recipe ? "own recipe" : "follows the default recipe"];
    if (photo.portrait) parts.push("portrait: letterboxed");
    if (photo.excluded) parts.push("excluded from the rotation");
    el.metaLine.textContent = parts.join(" · ");
  }

  function renderSelection() {
    const m = methods.find((x) => x.id === method) || methods[0];
    for (const [mid, t] of el.tiles) {
      const on = mid === method;
      t.el.setAttribute("aria-selected", on ? "true" : "false");
      t.el.tabIndex = on ? 0 : -1;
      t.el.classList.toggle("is-saved", !!saved && mid === saved.method);
    }
    el.desc.replaceChildren(h("strong", null, m.name), h("span", { class: "mono method-kind" }, KIND_LABELS[m.kind] || m.kind), h("span", null, m.description));
    el.capName.textContent = m.name;
    el.stageImg.alt = `${displayName(photo.original_name)}, dithered with ${m.name}`;
    // knobs that do nothing for this kind of method are dimmed, not hidden
    const diffusion = m.kind === "error_diffusion";
    el.toggles.querySelector('[data-knob="serpentine"]').classList.toggle("inert", !diffusion);
    root.querySelector('[data-knob="strength"]').classList.toggle("inert", m.kind === "nearest");
  }

  function renderControls() {
    for (const [k, s] of el.sliders) {
      s.input.value = String(knobs[k]);
      s.out.textContent = Number(knobs[k]).toFixed(2);
    }
    el.serp.checked = !!knobs.serpentine;
    el.dist.set(knobs.distance);
    el.pal.set(knobs.palette);
  }

  function renderDirty() {
    if (!el.saveBtn) return;
    const dirty = isDirty();
    el.saveBtn.disabled = !dirty || busySaving;
    el.savebar.hidden = !dirty || actionsInView;
    el.barSave.disabled = busySaving;
    el.showBtn.querySelector("span:last-child").textContent = dirty ? "Save & show on panel" : "Show on panel";
    el.resetBtn.disabled = busySaving || (!photo.recipe && !dirty);
    if (dirty) el.status.replaceChildren(h("span", { class: "badge badge-warn" }, "Unsaved"), " Changes to this photo's recipe");
    else el.status.textContent = photo.recipe ? `Saved with its own recipe: ${methodName(saved.method)}.` : `Follows the default recipe: ${methodName(saved.method)}.`;
    renderDefaultNote();
  }

  function renderDefaultNote() {
    if (followers === null) {
      el.defaultNote.textContent = "New uploads start with it, and so do the photos that follow the default.";
      return;
    }
    const n = followers.length;
    el.defaultNote.textContent = n
      ? `Also re-renders the ${plural(n, "photo")} that follow${n === 1 ? "s" : ""} the default, and new uploads start with it.`
      : "New uploads start with it. Every photo has its own recipe, so none of them change.";
  }

  function refreshBusy() {
    if (!el.bezel) return;
    const have = known.get(sig());
    const busy = stageLoading || previewSoon.pending() || (inflight && !(have && have.has(method)));
    el.bezel.classList.toggle("busy", busy);
  }

  function setStage(url) {
    if (!url || url === stageUrl) {
      refreshBusy();
      return;
    }
    stageUrl = url;
    stageLoading = true;
    refreshBusy();
    // preload so the old image stays up until the new one is ready
    const probe = new Image();
    probe.onload = () => {
      if (destroyed || stageUrl !== url) return;
      el.stageImg.src = url;
      stageLoading = false;
      refreshBusy();
    };
    probe.onerror = () => {
      if (destroyed || stageUrl !== url) return;
      stageLoading = false;
      refreshBusy();
    };
    probe.src = url;
  }

  function setZoom(value) {
    el.stageScroll.dataset.zoom = value;
    el.zoom.set(value);
    if (value !== "fit") {
      // centre the zoomed view on the detail the tiles show (reading
      // scrollWidth lays the zoomed image out first)
      const s = el.stageScroll;
      const scale = s.scrollWidth / 600;
      s.scrollLeft = focus.x * scale - s.clientWidth / 2;
      s.scrollTop = focus.y * scale - s.clientHeight / 2;
    }
  }

  function setStripView(value) {
    el.strip.dataset.view = value;
    el.view.set(value);
    el.stageScroll.classList.toggle("picking", value === "detail");
  }

  /** Move the detail the tiles show (centre in panel pixels). */
  function setFocus({ x, y }) {
    const cx = Math.round(Math.min(600 - DETAIL_W / 2, Math.max(DETAIL_W / 2, x)));
    const cy = Math.round(Math.min(400 - DETAIL_H / 2, Math.max(DETAIL_H / 2, y)));
    focus = { x: cx, y: cy };
    const x0 = cx - DETAIL_W / 2;
    const y0 = cy - DETAIL_H / 2;
    // the tile image is 600/72 tiles wide; shift it by the detail's corner
    el.strip.style.setProperty("--dx", String(x0 / DETAIL_W));
    el.strip.style.setProperty("--dy", String(y0 / DETAIL_H));
    Object.assign(el.focusBox.style, { left: `${(x0 / 600) * 100}%`, top: `${(y0 / 400) * 100}%`, width: `${(DETAIL_W / 600) * 100}%`, height: `${(DETAIL_H / 400) * 100}%` });
  }

  // Fit: a click or tap picks the detail.  Zoomed: drag with the mouse to pan
  // (touch screens scroll the window natively; the scrollbars are hidden).
  function wireStage() {
    const s = el.stageScroll;
    let pan = null;
    s.addEventListener("click", (e) => {
      if (s.dataset.zoom !== "fit" || el.strip.dataset.view !== "detail") return;
      const r = el.stageImg.getBoundingClientRect();
      if (!r.width) return;
      setFocus({ x: ((e.clientX - r.left) / r.width) * 600, y: ((e.clientY - r.top) / r.height) * 400 });
    });
    s.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "mouse" || e.button !== 0 || s.dataset.zoom === "fit") return;
      e.preventDefault();
      pan = { x: e.clientX, y: e.clientY, left: s.scrollLeft, top: s.scrollTop };
      try {
        s.setPointerCapture(e.pointerId);
      } catch {
        // a pointer that is already gone: the drag ends with the next event
      }
      s.classList.add("panning");
    });
    s.addEventListener("pointermove", (e) => {
      if (!pan) return;
      s.scrollLeft = pan.left - (e.clientX - pan.x);
      s.scrollTop = pan.top - (e.clientY - pan.y);
    });
    const end = () => {
      pan = null;
      s.classList.remove("panning");
    };
    s.addEventListener("pointerup", end);
    s.addEventListener("pointercancel", end);
  }

  // --- previews ---------------------------------------------------------------------------

  const previewSoon = debounce(() => pump(), PREVIEW_DEBOUNCE_MS);
  const stripSoon = debounce(() => {
    stripWanted = true;
    pump();
  }, STRIP_SETTLE_MS);

  function knobsChanged() {
    blocked = "";
    // the strip waits until the knobs settle; the stage goes first
    stripWanted = false;
    renderDirty();
    showSelected();
    previewSoon();
    stripSoon();
    markStrip();
    refreshBusy();
  }

  function remember(s, previews) {
    let have = known.get(s);
    if (!have) {
      have = new Map();
      known.set(s, have);
      // forget the oldest signatures; their URLs stay valid on the server
      while (known.size > KEEP_SIGS) known.delete(known.keys().next().value);
    }
    for (const p of previews) have.set(p.method, p);
    return have;
  }

  /** What to ask for next: the stage first, then the strip in chunks. */
  function nextRequest() {
    const s = sig();
    if (s === blocked) return null;
    const have = known.get(s);
    if (!have || !have.has(method)) return { s, want: [method] };
    if (!stripWanted) return null;
    const missing = methods.map((m) => m.id).filter((mid) => !have.has(mid));
    return missing.length ? { s, want: missing.slice(0, STRIP_CHUNK) } : null;
  }

  async function pump() {
    if (destroyed || inflight) return;
    const req = nextRequest();
    if (!req) {
      showSelected();
      applyStrip();
      refreshBusy();
      return;
    }
    inflight = true;
    refreshBusy();
    try {
      const body = { ...JSON.parse(req.s), methods: req.want };
      const res = await api(`/api/photos/${id}/previews`, { method: "POST", body });
      if (destroyed) return;
      remember(req.s, res.previews);
      // the caption times the stage's own render, the one a slider waits for
      if (req.want.length === 1 && req.want[0] === method) {
        el.took.textContent = res.took_ms < 1000 ? `${res.took_ms} ms` : `${(res.took_ms / 1000).toFixed(1)} s`;
        el.took.title = `Rendering ${methodName(method)} with these adjustments took ${res.took_ms} ms`;
      }
    } catch {
      // toasted by api(); do not retry these knobs until they change
      blocked = req.s;
    } finally {
      inflight = false;
      if (!destroyed) {
        showSelected();
        applyStrip();
        // a slider still moving: its debounce will pump when it settles
        if (!previewSoon.pending()) pump();
        refreshBusy();
      }
    }
  }

  function showSelected() {
    const have = known.get(sig());
    const p = have && have.get(method);
    if (p) setStage(p.url);
    refreshBusy();
  }

  function applyStrip() {
    const s = sig();
    const have = known.get(s);
    if (stripWanted && have) {
      for (const [mid, t] of el.tiles) {
        const p = have.get(mid);
        if (p && t.sig !== s) {
          t.sig = s;
          if (t.img.getAttribute("src") !== p.url) {
            t.el.classList.remove("loaded");
            t.img.src = p.url;
          }
        }
      }
    }
    if ([...el.tiles.values()].every((t) => t.sig === s)) stripWanted = false;
    markStrip();
  }

  /** Tiles that show older knobs than the stage are dimmed until they catch up. */
  function markStrip() {
    const s = sig();
    let stale = 0;
    for (const t of el.tiles.values()) {
      const old = !!t.sig && t.sig !== s;
      t.el.classList.toggle("stale", old);
      if (old) stale += 1;
    }
    el.stripNote.hidden = !stale;
    el.stripNote.textContent = inflight || stripSoon.pending() ? "updating" : "previous adjustments";
  }

  function refreshStrip() {
    stripSoon.cancel();
    blocked = "";
    stripWanted = true;
    applyStrip();
    pump();
  }

  function selectMethod(mid, { focus: takeFocus = false } = {}) {
    if (!el.tiles.has(mid)) return;
    method = mid;
    blocked = "";
    renderSelection();
    renderDirty();
    showSelected();
    // a tap is not a slider drag: ask straight away
    previewSoon.cancel();
    pump();
    const t = el.tiles.get(mid).el;
    if (takeFocus) t.focus({ preventScroll: true });
    // keep the tile in view inside the horizontal strip without moving the page
    const strip = el.strip;
    if (strip.scrollWidth > strip.clientWidth) {
      const left = t.offsetLeft - strip.offsetLeft;
      if (left < strip.scrollLeft || left + t.offsetWidth > strip.scrollLeft + strip.clientWidth) {
        strip.scrollTo({ left: left - (strip.clientWidth - t.offsetWidth) / 2, behavior: "smooth" });
      }
    }
  }

  // --- the method strip: click, arrows, long-press ------------------------------------------

  function wireTile(tileEl, m, tip) {
    let timer = 0;
    let longPressed = false;
    const hideTip = () => {
      tip.hidden = true;
    };
    tileEl.addEventListener("pointerdown", (e) => {
      if (e.pointerType !== "touch") return;
      longPressed = false;
      clearTimeout(timer);
      timer = setTimeout(() => {
        longPressed = true;
        tip.replaceChildren(h("strong", null, m.name), " ", m.description);
        tip.hidden = false;
        const wrap = tip.parentElement.getBoundingClientRect();
        const r = tileEl.getBoundingClientRect();
        tip.style.left = `${Math.max(0, Math.min(r.left - wrap.left, wrap.width - 260))}px`;
        tip.style.top = `${r.top - wrap.top - 8}px`;
      }, LONG_PRESS_MS);
    });
    const cancel = () => clearTimeout(timer);
    tileEl.addEventListener("pointerup", () => {
      cancel();
      if (longPressed) setTimeout(hideTip, 1600);
    });
    tileEl.addEventListener("pointercancel", () => {
      cancel();
      hideTip();
    });
    tileEl.addEventListener("pointerleave", cancel);
    tileEl.addEventListener("contextmenu", (e) => e.preventDefault());
    tileEl.addEventListener("click", (e) => {
      if (longPressed) {
        // the long-press showed the description; it is not a selection
        longPressed = false;
        e.preventDefault();
        return;
      }
      selectMethod(m.id);
    });
  }

  function onStripKey(e) {
    const ids = methods.map((m) => m.id);
    const i = ids.indexOf(method);
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = Math.min(ids.length - 1, i + 1);
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = Math.max(0, i - 1);
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = ids.length - 1;
    if (next >= 0) {
      e.preventDefault();
      selectMethod(ids[next], { focus: true });
    }
  }

  function onKey(e) {
    // the strip's own handler has dealt with arrows pressed inside it
    if (e.defaultPrevented || document.querySelector("dialog[open]")) return;
    const tag = (e.target && e.target.tagName) || "";
    const typing = tag === "INPUT" && e.target.type !== "range" && e.target.type !== "checkbox" && e.target.type !== "radio";
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (isDirty()) save();
      return;
    }
    if (typing || tag === "TEXTAREA" || tag === "SELECT" || e.ctrlKey || e.metaKey || e.altKey) return;
    const key = e.key.toLowerCase();
    if (key === "z") {
      const order = ["fit", "1", "2"];
      setZoom(order[(order.indexOf(el.stageScroll.dataset.zoom) + 1) % order.length]);
    } else if (key === "d") {
      setStripView(el.strip.dataset.view === "detail" ? "whole" : "detail");
    } else if (key === "r") {
      refreshStrip();
    } else if ((e.key === "ArrowRight" || e.key === "ArrowLeft") && tag !== "INPUT") {
      onStripKey(e);
    } else if (e.key === "Escape") {
      location.hash = "#/photos";
    }
  }

  // --- actions ------------------------------------------------------------------------------

  function adopt(updated) {
    photo = updated;
    saved = { method: photo.recipe_effective.method, ...pickKnobs(photo.recipe_effective) };
    renderMeta();
    renderSelection();
    renderDirty();
  }

  async function loadFollowers() {
    try {
      const list = await api("/api/photos", { quiet: true });
      if (destroyed) return null;
      followers = list.photos.filter((p) => !p.recipe);
      renderDefaultNote();
    } catch {
      // the note falls back to a sentence without the count
    }
    return followers;
  }

  async function save() {
    if (busySaving) return false;
    busySaving = true;
    renderDirty();
    try {
      const updated = await api(`/api/photos/${id}/recipe`, { method: "PUT", body: { method, ...knobs } });
      const wasFollowing = !photo.recipe;
      adopt(updated);
      if (wasFollowing && followers) followers = followers.filter((p) => p.id !== id);
      toast(`Saved: ${displayName(photo.original_name)} renders with ${methodName(method)}.`, "ok");
      return true;
    } catch {
      return false;
    } finally {
      busySaving = false;
      renderDirty();
    }
  }

  // The default recipe applies to every photo without its own recipe, not
  // only to new uploads, so the confirm names how many photos change.
  /** "Atkinson → Floyd–Steinberg, contrast 1.00 → 1.35": what would change. */
  function describeChange(from) {
    const parts = [];
    if (from.method !== method) parts.push(`${methodName(from.method)} → ${methodName(method)}`);
    for (const k of KNOB_KEYS) {
      if (from[k] === knobs[k]) continue;
      const show = (v) => (typeof v === "number" ? v.toFixed(2) : typeof v === "boolean" ? (v ? "on" : "off") : v);
      parts.push(`${k} ${show(from[k])} → ${show(knobs[k])}`);
    }
    return parts.join(", ");
  }

  async function saveAsDefault() {
    const name = methodName(method);
    const list = await loadFollowers();
    const current = await getRecipeMeta({ fresh: true }).catch(() => null);
    const change = current ? describeChange(current) : "";
    if (current && !change) {
      toast(`This is the default recipe already (${name} with these adjustments).`, "info");
      return;
    }
    const n = list ? list.length : null;
    const others = list ? list.filter((p) => p.id !== id).length : null;
    const lines = [];
    if (change) lines.push(`The default becomes: ${change}.`);
    if (n === null) lines.push("Every photo that follows the default re-renders with it, and new uploads start with it.");
    else if (n === 0) lines.push("New uploads start with it. Every photo has its own recipe, so none of them change.");
    else lines.push(`${plural(n, "photo")} that follow${n === 1 ? "s" : ""} the default${photo.recipe ? "" : others ? ", this one included," : " (this one)"} re-render${n === 1 ? "s" : ""} with it, and new uploads start with it.`);
    if (photo.recipe) lines.push("This photo keeps its own recipe.");
    lines.push("The old default is not kept: there is no undo.");
    const ok = await confirmDialog({
      title: "Make this the default recipe?",
      message: lines.join(" "),
      confirm: n ? `Change ${plural(n, "photo")}` : "Make it the default",
      cancel: "Keep the old default",
    });
    if (!ok) return;
    try {
      const fresh = await api("/api/recipe/default", { method: "PUT", body: { method, ...knobs } });
      setRecipeMeta(fresh);
      // a photo without its own recipe follows the default, so it changed too
      if (!photo.recipe) adopt(await api(`/api/photos/${id}`));
      toast(n ? `Default set to ${name}: ${plural(n, "photo")} and new uploads use it.` : `Default set to ${name} for new uploads.`, "ok");
    } catch {
      // toasted
    }
  }

  async function reset() {
    busySaving = true;
    renderDirty();
    try {
      const updated = await api(`/api/photos/${id}/recipe`, { method: "PUT", body: { reset: true } });
      method = updated.recipe_effective.method;
      knobs = pickKnobs(updated.recipe_effective);
      adopt(updated);
      renderControls();
      if (followers && !followers.some((p) => p.id === id)) followers = [...followers, updated];
      stripWanted = true;
      previewSoon.cancel();
      markStrip();
      pump();
      toast("Reset: the photo follows the default recipe again.", "ok");
    } catch {
      // toasted
    } finally {
      busySaving = false;
      renderDirty();
    }
  }

  async function showOnPanel() {
    if (isDirty() && !(await save())) return;
    try {
      const result = await api("/api/display/show", { method: "POST", body: { photo_id: id } });
      toast(queuedMessage(result, displayName(photo.original_name)), "ok");
    } catch {
      // toasted
    }
  }

  // --- load -------------------------------------------------------------------------------

  Promise.all([api(`/api/photos/${id}`, { quiet: true }), getRecipeMeta()])
    .then(([p, m]) => {
      if (destroyed) return;
      photo = p;
      meta = m;
      methods = m.methods;
      method = p.recipe_effective.method;
      knobs = pickKnobs(p.recipe_effective);
      saved = { method, ...knobs };
      build();
      renderMeta();
      renderSelection();
      renderDirty();
      setStripView("detail");
      // the stored render is exactly the saved recipe: show it straight away
      setStage(p.render_url);
      pump();
      loadFollowers();
      document.addEventListener("keydown", onKey);
    })
    .catch((e) => {
      if (destroyed) return;
      const missing = e && e.status === 404;
      root.replaceChildren(
        h("a", { class: "back", href: "#/photos" }, icon("back"), "Photos"),
        emptyState(missing ? "This photo is gone" : "Could not open the photo", missing ? "It may have been deleted on another device." : (e && e.message) || "The frame did not answer."),
      );
    });

  return {
    destroy() {
      destroyed = true;
      previewSoon.cancel();
      stripSoon.cancel();
      for (const o of observers) o.disconnect();
      document.removeEventListener("keydown", onKey);
    },
    isDirty,
  };
}

// Rotation: the ordered slots, quiet hours and the panel-care timings.
// Edits are a local draft until Save, which sends PUT /api/settings (quiet
// hours, panel, display) and then PUT /api/rotation (the slot list).  A 422
// answer names the field ("rotation[2].duration_minutes ...",
// "quiet_hours.start: ..."); the message is shown under that field.

import { api } from "./api.js";
import { describeDays } from "./now.js";
import { emptyState, eyebrow, fmtHM, fmtMinutes, h, icon, loading, pageHead, segmented, setChildren, switchInput, toast } from "./ui.js";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const clone = (v) => JSON.parse(JSON.stringify(v));

export function rotationView(root) {
  let destroyed = false;
  let server = null; // {rotation, settings} as last loaded or saved
  let draft = null; // {slots, quiet, panel, clear}
  let apps = []; // [{id, name, description}]
  let saving = false;
  const fields = new Map(); // error path -> element to mark

  const head = pageHead("Rotation", "What the frame cycles through, in this order, and when it rests");
  const body = h("div", { class: "rotation-body" }, loading("Loading the rotation"));
  const saveMsg = h("p", { class: "savebar-msg", role: "status" });
  const discardBtn = h("button", { class: "btn btn-quiet", type: "button", onclick: discard }, "Discard");
  const saveBtn = h("button", { class: "btn btn-primary", type: "button", onclick: save }, icon("check"), h("span", null, "Save"));
  const savebar = h("div", { class: "savebar", hidden: true }, saveMsg, h("div", { class: "savebar-actions" }, discardBtn, saveBtn));
  root.append(head, body, savebar);

  const isDirty = () => !!draft && JSON.stringify(draftPayload()) !== JSON.stringify(serverPayload());

  function serverPayload() {
    const s = server.settings;
    return {
      slots: server.rotation.slots.map(slotBody),
      quiet: s.quiet_hours,
      panel: { refresh_floor_seconds: s.panel.refresh_floor_seconds, keepalive_hours: s.panel.keepalive_hours },
      clear: s.display.clear_before_refresh,
    };
  }

  function draftPayload() {
    return { slots: draft.slots.map(slotBody), quiet: draft.quiet, panel: draft.panel, clear: draft.clear };
  }

  function slotBody(slot) {
    return { app_id: slot.app_id, enabled: slot.enabled, duration_minutes: slot.duration_minutes, options: slot.options || {} };
  }

  function fromServer() {
    const s = server.settings;
    draft = {
      slots: clone(server.rotation.slots),
      quiet: clone(s.quiet_hours),
      panel: { refresh_floor_seconds: s.panel.refresh_floor_seconds, keepalive_hours: s.panel.keepalive_hours },
      clear: s.display.clear_before_refresh,
    };
  }

  function changed() {
    const dirty = isDirty();
    savebar.hidden = !dirty && !saveMsg.classList.contains("error");
    if (dirty && !saveMsg.classList.contains("error")) saveMsg.textContent = "Unsaved changes";
    saveBtn.disabled = saving || !dirty;
    updateTotal();
  }

  // --- slots ----------------------------------------------------------------------------

  const total = h("span", { class: "mono eyebrow-note" });
  const list = h("ol", { class: "slots" });
  const addWrap = h("div", { class: "slot-add" });

  // one cycle drawn to scale: a segment per enabled slot, and for the photos
  // slot a tick at every photo change.  Each segment shows as much as fits
  // without cutting a word: name and minutes, else the name alone, else the
  // slot's number from the list below ("02"), else nothing (a sliver); the
  // title has the details either way.
  const timeline = h("div", { class: "cycle", "aria-hidden": "true" });
  const NUMBER_MIN_PX = 26;
  const LEVELS = ["no-min", "narrow", "sliver"];

  function labelTimeline() {
    const segs = [...timeline.children];
    if (!segs.length || !timeline.clientWidth) return;
    for (const seg of segs) seg.classList.remove(...LEVELS);
    // read everything first, then write, so the layout is computed once
    const fits = (el) => !el || el.scrollWidth <= el.clientWidth + 1;
    const levels = segs.map((seg) => {
      if (seg.clientWidth < NUMBER_MIN_PX) return "sliver";
      const label = seg.querySelector(".cycle-label");
      if (!fits(label) || !fits(seg.querySelector(".cycle-name"))) return "narrow";
      return fits(seg.querySelector(".cycle-min")) ? "" : "no-min";
    });
    segs.forEach((seg, i) => {
      if (levels[i]) seg.classList.add(levels[i]);
    });
  }
  const timelineSize = "ResizeObserver" in window ? new ResizeObserver(labelTimeline) : null;
  if (timelineSize) timelineSize.observe(timeline);

  function updateTotal() {
    if (!draft) return;
    const on = draft.slots.map((s, i) => ({ s, i })).filter(({ s }) => s.enabled && Number(s.duration_minutes) > 0);
    const mins = on.reduce((a, { s }) => a + Number(s.duration_minutes), 0);
    total.textContent = mins ? `one cycle ≈ ${fmtMinutes(mins)}` : "nothing enabled";
    setChildren(timeline, on.map(({ s, i }) => {
      const dur = Number(s.duration_minutes);
      const name = s.name || s.app_id;
      const seg = h("div", { class: "cycle-seg", style: { flexGrow: String(dur) }, title: `${String(i + 1).padStart(2, "0")} ${name}: ${fmtMinutes(dur)}` },
        h("span", { class: "cycle-label" }, h("span", { class: "cycle-num mono" }, String(i + 1).padStart(2, "0")), h("span", { class: "cycle-name" }, name)),
        h("span", { class: "cycle-min mono" }, fmtMinutes(dur)));
      const dwell = s.app_id === "photos" && s.options ? Number(s.options.dwell_minutes) : 0;
      if (dwell > 0 && dwell < dur) {
        for (let t = dwell; t < dur && t / dwell < 60; t += dwell) {
          seg.append(h("i", { class: "cycle-tick", style: { left: `${(t / dur) * 100}%` } }));
        }
      }
      return seg;
    }));
    timeline.hidden = !on.length;
    labelTimeline();
  }

  function numberField({ value, min, max, step = 1, label, suffix, onchange, path, cls = "" }) {
    const input = h("input", { type: "number", inputmode: "numeric", class: `num ${cls}`.trim(), min, max, step, value, "aria-label": label });
    input.addEventListener("input", () => {
      clearFieldError(input);
      const v = input.value === "" ? NaN : Number(input.value);
      onchange(Number.isFinite(v) ? v : input.value);
      changed();
    });
    if (path) fields.set(path, input);
    return h("label", { class: "inline-field" }, input, suffix ? h("span", { class: "inline-suffix" }, suffix) : null);
  }

  function renderSlots(focusSel) {
    fields.forEach((_, k) => {
      if (k.startsWith("rotation[")) fields.delete(k);
    });
    list.replaceChildren(...draft.slots.map((slot, i) => slotRow(slot, i)));
    const present = new Set(draft.slots.map((s) => s.app_id));
    const missing = apps.filter((a) => !present.has(a.id));
    addWrap.replaceChildren();
    if (missing.length) {
      const select = h("select", { class: "select", "aria-label": "App to add" }, missing.map((a) => h("option", { value: a.id }, a.name)));
      addWrap.append(select, h("button", { class: "btn btn-sm", type: "button", onclick: () => {
        const app = apps.find((a) => a.id === select.value);
        if (!app) return;
        const slot = { app_id: app.id, enabled: true, duration_minutes: 3, options: app.id === "photos" ? { dwell_minutes: 3, order: "shuffle" } : {}, name: app.name, description: app.description };
        draft.slots.push(slot);
        renderSlots();
        changed();
      } }, icon("plus"), h("span", null, "Add to the rotation")));
    }
    if (focusSel) {
      const target = list.querySelector(focusSel);
      if (target) target.focus();
    }
    updateTotal();
  }

  function move(from, to, focusSel) {
    if (to < 0 || to >= draft.slots.length || from === to) return;
    const [slot] = draft.slots.splice(from, 1);
    draft.slots.splice(to, 0, slot);
    renderSlots(focusSel && focusSel(to));
    changed();
  }

  function slotRow(slot, i) {
    const n = draft.slots.length;
    const name = slot.name || slot.app_id;
    const handle = h("button", { class: "handle", type: "button", "aria-label": `Reorder ${name}: drag, or use the arrow keys`, title: "Drag to reorder" }, icon("grip"));
    const toggle = switchInput({ checked: slot.enabled, label: `Show ${name} in the rotation`, onchange: (v) => { slot.enabled = v; row.classList.toggle("off", !v); changed(); } });
    const up = h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Move ${name} up`, title: "Move up", disabled: i === 0, dataset: { act: "up" }, onclick: () => move(i, i - 1, (to) => `li:nth-child(${to + 1}) [data-act="${to === 0 ? "down" : "up"}"]`) }, icon("up"));
    const down = h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Move ${name} down`, title: "Move down", disabled: i === n - 1, dataset: { act: "down" }, onclick: () => move(i, i + 1, (to) => `li:nth-child(${to + 1}) [data-act="${to === n - 1 ? "up" : "down"}"]`) }, icon("down"));

    const fieldsRow = h("div", { class: "slot-fields" },
      h("span", { class: "field-label" }, "Show for"),
      numberField({ value: slot.duration_minutes, min: 1, max: 1440, label: `Minutes to show ${name}`, suffix: "min", path: `rotation[${i}].duration_minutes`, onchange: (v) => { slot.duration_minutes = v; } }),
      h("div", { class: "slot-move" }, up, down));
    const extra = [];
    if (slot.app_id === "photos") {
      slot.options = { dwell_minutes: 3, order: "shuffle", ...(slot.options || {}) };
      const order = segmented({
        label: "Photo order", small: true, value: slot.options.order,
        options: [{ value: "shuffle", label: "Shuffle" }, { value: "sequential", label: "In order" }],
        onchange: (v) => { slot.options.order = v; changed(); },
      });
      fields.set(`rotation[${i}].options.order`, order.el);
      extra.push(h("div", { class: "slot-fields" },
        h("span", { class: "field-label" }, "Each photo"),
        numberField({ value: slot.options.dwell_minutes, min: 1, max: 1440, label: "Minutes per photo", suffix: "min", path: `rotation[${i}].options.dwell_minutes`, onchange: (v) => { slot.options.dwell_minutes = v; } }),
        order.el));
    }
    const row = h("li", { class: `slot${slot.enabled ? "" : " off"}`, dataset: { index: String(i) } },
      handle,
      h("div", { class: "slot-title" }, h("span", { class: "slot-num mono" }, String(i + 1).padStart(2, "0")), h("span", { class: "slot-name" }, name)),
      h("div", { class: "slot-toggle" }, toggle),
      h("div", { class: "slot-main" },
        slot.description ? h("p", { class: "slot-desc" }, slot.description) : null,
        fieldsRow, extra,
        h("p", { class: "field-error", hidden: true })));
    fields.set(`rotation[${i}]`, row);
    wireDrag(handle, row);
    handle.addEventListener("keydown", (e) => {
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        const to = e.key === "ArrowUp" ? i - 1 : i + 1;
        move(i, to, (t) => `li:nth-child(${t + 1}) .handle`);
      }
    });
    return row;
  }

  // Drag to reorder with pointer events.  The dragged row follows the
  // pointer with a transform; whenever its centre passes a neighbour's
  // centre the two swap places in the DOM, and the transform is corrected
  // by the neighbour's height so the row stays under the finger.  The new
  // order is read back from the DOM on release.
  //
  // The handle has touch-action: none (the finger drags, it does not
  // scroll), so near the top or bottom of the visible area the page scrolls
  // by itself, faster the closer the finger is to the edge; the row's
  // offset counts the scrolled distance so it stays under the finger.
  const EDGE_PX = 64;
  const MAX_SCROLL_PX = 18;

  /** The part of the window not covered by the nav, tab bar or save bar. */
  function visibleBand() {
    let top = 0;
    let bottom = window.innerHeight;
    const nav = document.querySelector(".nav");
    if (nav) {
      const r = nav.getBoundingClientRect();
      if (r.top > window.innerHeight / 2) bottom = Math.min(bottom, r.top);
      else top = Math.max(top, r.bottom);
    }
    if (!savebar.hidden) bottom = Math.min(bottom, savebar.getBoundingClientRect().top);
    return { top, bottom };
  }

  function wireDrag(handle, row) {
    handle.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      try {
        handle.setPointerCapture(e.pointerId);
      } catch {
        // a pointer that is already gone: the moves simply stop arriving
      }
      const startY = e.clientY;
      const startScroll = window.scrollY;
      const band = visibleBand();
      let pointerY = e.clientY;
      let shift = 0; // how far the row's layout slot has moved since the start
      let moved = false;
      let raf = 0;
      row.classList.add("dragging");
      list.classList.add("sorting");

      const offset = () => pointerY - startY + (window.scrollY - startScroll);
      const reorder = () => {
        // several swaps may be due at once after a fast scroll
        for (let guard = 0; guard < 24; guard += 1) {
          const r = row.getBoundingClientRect();
          const centre = r.top + r.height / 2;
          const next = row.nextElementSibling;
          const prev = row.previousElementSibling;
          const nr = next && next.getBoundingClientRect();
          const pr = prev && prev.getBoundingClientRect();
          if (nr && centre > nr.top + nr.height / 2) {
            list.insertBefore(next, row);
            shift += nr.height;
          } else if (pr && centre < pr.top + pr.height / 2) {
            list.insertBefore(row, prev);
            shift -= pr.height;
          } else {
            return;
          }
          row.style.transform = `translateY(${offset() - shift}px)`;
        }
      };
      const place = () => {
        const dy = offset();
        if (Math.abs(dy) > 3) moved = true;
        row.style.transform = `translateY(${dy - shift}px)`;
        reorder();
      };
      // speed in px per 60 Hz frame, scaled by the real frame time so a
      // 120 Hz screen scrolls no faster
      let lastFrame = 0;
      const autoScroll = (now) => {
        raf = 0;
        let v = 0;
        if (pointerY < band.top + EDGE_PX) v = -(band.top + EDGE_PX - pointerY) / 3;
        else if (pointerY > band.bottom - EDGE_PX) v = (pointerY - (band.bottom - EDGE_PX)) / 3;
        v = Math.max(-MAX_SCROLL_PX, Math.min(MAX_SCROLL_PX, v));
        if (!v) {
          lastFrame = 0;
          return;
        }
        const dt = lastFrame ? Math.min(50, now - lastFrame) : 16.7;
        lastFrame = now;
        const before = window.scrollY;
        window.scrollBy(0, Math.round((v * dt) / 16.7));
        if (window.scrollY !== before) place();
        raf = requestAnimationFrame(autoScroll);
      };

      const onMove = (ev) => {
        pointerY = ev.clientY;
        place();
        if (!raf) raf = requestAnimationFrame(autoScroll);
      };
      const onUp = () => {
        cancelAnimationFrame(raf);
        raf = 0;
        handle.removeEventListener("pointermove", onMove);
        handle.removeEventListener("pointerup", onUp);
        handle.removeEventListener("pointercancel", onUp);
        row.classList.remove("dragging");
        list.classList.remove("sorting");
        row.style.transform = "";
        if (!moved) return;
        const order = [...list.children].map((li) => Number(li.dataset.index));
        const before = draft.slots;
        draft.slots = order.map((idx) => before[idx]);
        renderSlots();
        changed();
      };
      handle.addEventListener("pointermove", onMove);
      handle.addEventListener("pointerup", onUp);
      handle.addEventListener("pointercancel", onUp);
    });
  }

  // --- quiet hours and panel care ---------------------------------------------------------

  function quietSection() {
    const q = draft.quiet;
    const start = h("input", { type: "time", class: "time", value: q.start, "aria-label": "Quiet hours start", required: true });
    const end = h("input", { type: "time", class: "time", value: q.end, "aria-label": "Quiet hours end", required: true });
    fields.set("quiet_hours.start", start);
    fields.set("quiet_hours.end", end);
    const summary = h("p", { class: "section-hint" });
    const updateSummary = () => {
      summary.textContent = q.enabled
        ? `No scheduled refreshes from ${fmtHM(q.start)} to ${fmtHM(q.end)}, starting ${describeDays(q.days)}. The last image stays up; show-now still works, and the keep-alive still fires.`
        : "Off: the rotation runs around the clock.";
    };
    for (const [input, key] of [[start, "start"], [end, "end"]]) {
      input.addEventListener("input", () => {
        clearFieldError(input);
        q[key] = input.value;
        updateSummary();
        changed();
      });
    }
    const enabled = switchInput({ checked: q.enabled, label: "Quiet hours", id: "quiet-enabled", onchange: (v) => { q.enabled = v; block.classList.toggle("off", !v); updateSummary(); changed(); } });
    const chips = h("div", { class: "chips", role: "group", "aria-label": "Days the quiet window starts" },
      DAYS.map((d, i) => {
        const chip = h("button", { class: "chip", type: "button", "aria-pressed": q.days.includes(i) ? "true" : "false" }, d);
        chip.addEventListener("click", () => {
          const on = q.days.includes(i);
          q.days = on ? q.days.filter((x) => x !== i) : [...q.days, i].sort();
          chip.setAttribute("aria-pressed", on ? "false" : "true");
          clearFieldError(chips);
          updateSummary();
          changed();
        });
        return chip;
      }));
    fields.set("quiet_hours.days", chips);
    fields.set("quiet_hours", chips);
    updateSummary();
    summary.classList.add("form-summary");
    const block = h("div", { class: `form-block${q.enabled ? "" : " off"}` },
      h("div", { class: "row-field" }, h("label", { for: "quiet-enabled" }, "Quiet hours"), enabled),
      h("div", { class: "row-field row-field-tight" }, h("span", { class: "field-label" }, "From"), h("div", { class: "time-range" }, start, h("span", { class: "muted" }, "to"), end)),
      fieldError(start), fieldError(end),
      h("div", { class: "row-field row-field-stack" }, h("span", { class: "field-label" }, "Starting on"), chips),
      fieldError(chips),
      summary);
    return h("section", { class: "section" }, eyebrow("Quiet hours"), block);
  }

  function panelSection() {
    const p = draft.panel;
    // both rows: a label with its help, then a short number and a one-letter
    // unit, so they line up (the floor's minutes go in its help text)
    const floorHelp = h("span", { class: "field-help" });
    const updateHints = () => {
      const f = Number(p.refresh_floor_seconds);
      const asMinutes = Number.isFinite(f) && f >= 60 ? ` (now ${fmtMinutes(Math.floor(f / 60))}${f % 60 ? ` ${f % 60} s` : ""})` : "";
      floorHelp.textContent = `The least time between scheduled refreshes${asMinutes}. The panel likes at least 180 s.`;
    };
    const floor = h("input", { type: "number", inputmode: "numeric", class: "num", min: 30, max: 86400, step: 30, value: p.refresh_floor_seconds, "aria-label": "Refresh floor in seconds" });
    const keep = h("input", { type: "number", inputmode: "numeric", class: "num", min: 1, max: 24, step: 1, value: p.keepalive_hours, "aria-label": "Keep-alive interval in hours" });
    fields.set("panel.refresh_floor_seconds", floor);
    fields.set("panel.keepalive_hours", keep);
    fields.set("panel.show_now_min_seconds", floor);
    floor.addEventListener("input", () => { clearFieldError(floor); p.refresh_floor_seconds = floor.value === "" ? floor.value : Number(floor.value); updateHints(); changed(); });
    keep.addEventListener("input", () => { clearFieldError(keep); p.keepalive_hours = keep.value === "" ? keep.value : Number(keep.value); changed(); });
    updateHints();
    const clear = switchInput({ checked: draft.clear, label: "Clear before refresh", id: "clear-before", onchange: (v) => { draft.clear = v; changed(); } });
    fields.set("display.clear_before_refresh", clear);
    return h("section", { class: "section" }, eyebrow("Panel care"),
      h("div", { class: "form-block" },
        h("div", { class: "row-field" }, h("label", null, "Refresh floor", floorHelp), h("label", { class: "inline-field" }, floor, h("span", { class: "inline-suffix unit" }, "s"))),
        fieldError(floor),
        h("div", { class: "row-field" }, h("label", null, "Keep-alive", h("span", { class: "field-help" }, "Refresh at least this often, even in quiet hours, so the image does not fade.")), h("label", { class: "inline-field" }, keep, h("span", { class: "inline-suffix unit" }, "h"))),
        fieldError(keep),
        h("div", { class: "row-field" }, h("label", { for: "clear-before" }, "Clear before refresh", h("span", { class: "field-help" }, "Flood the panel white first: less ghosting, but each refresh takes about twice as long.")), clear)));
  }

  // --- errors ---------------------------------------------------------------------------------

  // a <p class="field-error"> placed right after a field's row
  const errorSlots = new WeakMap();
  function fieldError(target) {
    const p = h("p", { class: "field-error", hidden: true });
    errorSlots.set(target, p);
    return p;
  }

  function clearFieldError(target) {
    target.classList.remove("invalid");
    target.removeAttribute("aria-invalid");
    const slotRowEl = target.closest && target.closest(".slot");
    const p = errorSlots.get(target) || (slotRowEl && slotRowEl.querySelector(".field-error"));
    if (p) p.hidden = true;
    if (saveMsg.classList.contains("error")) {
      saveMsg.classList.remove("error");
      changed();
    }
  }

  function clearErrors() {
    for (const node of root.querySelectorAll(".invalid")) node.classList.remove("invalid");
    for (const node of root.querySelectorAll(".field-error")) node.hidden = true;
    saveMsg.classList.remove("error");
  }

  /** Put a 422 message under the field it names; the longest known path wins. */
  function showError(message) {
    const m = message.match(/^([A-Za-z_][\w.[\]]*)/);
    const named = m ? m[1].replace(/^slots\[/, "rotation[") : "";
    let best = "";
    for (const path of fields.keys()) {
      if ((named === path || named.startsWith(`${path}.`) || named.startsWith(`${path}[`) || named.startsWith(`${path}:`)) && path.length > best.length) best = path;
    }
    // "rotation[0].duration_minutes must be ..." reads better as "Must be ..."
    // under the field itself; other messages are shown as they are
    let human = message;
    if (m && /^(:\s*|\s+must\b)/.test(message.slice(m[1].length))) {
      const rest = message.slice(m[1].length).replace(/^:\s*/, "").trim();
      human = rest.charAt(0).toUpperCase() + rest.slice(1);
    }
    saveMsg.textContent = `Not saved: ${best ? human.charAt(0).toLowerCase() + human.slice(1) : message}`;
    saveMsg.classList.add("error");
    savebar.hidden = false;
    if (!best) return;
    message = human;
    const target = fields.get(best);
    target.classList.add("invalid");
    target.setAttribute("aria-invalid", "true");
    const slotRowEl = target.closest(".slot");
    const p = errorSlots.get(target) || (slotRowEl && slotRowEl.querySelector(".field-error"));
    if (p) {
      p.textContent = message;
      p.hidden = false;
    }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    if (typeof target.focus === "function" && target.tagName !== "LI" && target.tagName !== "DIV") target.focus({ preventScroll: true });
  }

  // --- save / load ------------------------------------------------------------------------------

  async function save() {
    if (saving) return;
    clearErrors();
    saving = true;
    saveBtn.disabled = true;
    saveMsg.textContent = "Saving…";
    try {
      const settings = await api("/api/settings", {
        method: "PUT",
        quiet: true,
        body: {
          quiet_hours: draft.quiet,
          panel: draft.panel,
          display: { clear_before_refresh: draft.clear },
        },
      });
      // remember what is saved already, in case the slot list is refused
      server = { ...server, settings };
      const rotation = await api("/api/rotation", { method: "PUT", quiet: true, body: { slots: draft.slots.map(slotBody) } });
      server = { settings, rotation };
      fromServer();
      render();
      toast("Rotation saved. The frame picks it up right away.", "ok");
    } catch (e) {
      if (e && e.status === 422) showError(e.message);
      else {
        saveMsg.textContent = e && e.message ? e.message : "Saving failed.";
        saveMsg.classList.add("error");
        toast(saveMsg.textContent, "error");
      }
    } finally {
      saving = false;
      changed();
    }
  }

  function discard() {
    clearErrors();
    fromServer();
    render();
  }

  function render() {
    fields.clear();
    renderSlots();
    body.replaceChildren(
      h("section", { class: "section rotation-slots" }, eyebrow("Slots", total), timeline, h("p", { class: "section-hint" }, "Drag the handle, or use the arrows, to change the order. Switched-off slots are skipped."), list, addWrap),
      h("div", { class: "rotation-side" }, quietSection(), panelSection()));
    changed();
  }

  Promise.all([api("/api/rotation"), api("/api/settings"), api("/api/apps").catch(() => [])])
    .then(([rotation, settings, appList]) => {
      if (destroyed) return;
      server = { rotation, settings };
      apps = appList.map((a) => ({ id: a.id, name: a.name, description: a.description }));
      fromServer();
      render();
    })
    .catch(() => {
      if (!destroyed) body.replaceChildren(emptyState("Could not load the rotation", "The frame did not answer. Check that it is on the network, then reload."));
    });

  return {
    destroy() {
      destroyed = true;
      if (timelineSize) timelineSize.disconnect();
    },
    isDirty,
  };
}

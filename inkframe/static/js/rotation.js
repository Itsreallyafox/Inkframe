// Rotation: the photo stream, the cards, quiet hours and the panel-care timings.
// One cycle is N photos in a row (each for a few minutes), then every enabled
// card once in list order, then photos again.  Edits are a local draft until
// Save, which sends PUT /api/settings (quiet hours, panel, display) and then
// PUT /api/rotation (photos, apps).  A 422 answer names the field
// ("rotation.apps[2].minutes ...", "rotation.photos.in_a_row ...",
// "quiet_hours.start: ..."); the message is shown under that field.

import { api } from "./api.js";
import { describeDays } from "./now.js";
import { emptyState, eyebrow, fmtHM, fmtMinutes, h, icon, loading, pageHead, segmented, setChildren, switchInput, toast } from "./ui.js";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MAX_IN_A_ROW = 200;
const MAX_MINUTES = 1440;
const clone = (v) => JSON.parse(JSON.stringify(v));
const NB = " ";

export function rotationView(root) {
  let destroyed = false;
  let server = null; // {rotation, settings} as last loaded or saved
  let draft = null; // {photos, apps, quiet, panel, clear}
  let saving = false;
  const fields = new Map(); // error path -> element to mark

  const head = pageHead("Rotation", "Photos in a row, then the cards, then photos again; and when the frame rests");
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
      photos: photosBody(server.rotation.photos),
      apps: server.rotation.apps.map(turnBody),
      quiet: s.quiet_hours,
      panel: { refresh_floor_seconds: s.panel.refresh_floor_seconds, keepalive_hours: s.panel.keepalive_hours },
      clear: s.display.clear_before_refresh,
    };
  }

  function draftPayload() {
    return { photos: photosBody(draft.photos), apps: draft.apps.map(turnBody), quiet: draft.quiet, panel: draft.panel, clear: draft.clear };
  }

  function photosBody(p) {
    return { enabled: p.enabled, in_a_row: p.in_a_row, minutes_each: p.minutes_each, order: p.order };
  }

  function turnBody(t) {
    return { app_id: t.app_id, enabled: t.enabled, minutes: t.minutes };
  }

  function fromServer() {
    const s = server.settings;
    draft = {
      photos: clone(server.rotation.photos),
      apps: clone(server.rotation.apps),
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
    updateCycle();
  }

  // --- the cycle: live numbers and the strip ----------------------------------------------------

  const total = h("span", { class: "mono eyebrow-note" });
  const summary = h("p", { class: "section-hint cycle-summary" });

  // one cycle drawn to scale: the photo run (a tick per photo change), then
  // a segment per enabled card.  Each segment shows as much as fits without
  // cutting a word: name and minutes, else the name alone, else the card's
  // number from the list below ("02"), else nothing (a sliver); the title
  // has the details either way.
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

  /** The cycle's numbers from the draft: photo minutes, card minutes, the enabled cards. */
  function cycleNumbers() {
    const p = draft.photos;
    const n = Number(p.in_a_row);
    const each = Number(p.minutes_each);
    const photoMins = p.enabled && n > 0 && each > 0 ? n * each : 0;
    const cards = draft.apps.map((t, i) => ({ t, i })).filter(({ t }) => t.enabled && Number(t.minutes) > 0);
    const cardMins = cards.reduce((a, { t }) => a + Number(t.minutes), 0);
    return { n, each, photoMins, cards, cardMins, total: photoMins + cardMins };
  }

  function updateCycle() {
    if (!draft) return;
    const c = cycleNumbers();
    // never a rounded "100%" while cards are in the cycle, nor "0%" while photos are
    const share = c.total ? Math.min(c.cardMins ? 99 : 100, Math.max(c.photoMins ? 1 : 0, Math.round((c.photoMins / c.total) * 100))) : 0;
    total.textContent = c.total ? `one cycle ≈ ${fmtMinutes(c.total)} · ${share}%${NB}photos` : "nothing enabled";
    if (!c.total) summary.textContent = "Nothing is enabled: the frame only keeps its last image alive.";
    else if (!c.photoMins) summary.textContent = `Cards ${fmtMinutes(c.cardMins)} · each card comes round every ${fmtMinutes(c.total)}.`;
    else if (!c.cards.length) summary.textContent = `Photos only: a new photo every ${fmtMinutes(c.each)}.`;
    else summary.textContent = `Photos ${fmtMinutes(c.photoMins)} (${share}%), cards ${fmtMinutes(c.cardMins)} · each card comes round every ${fmtMinutes(c.total)}.`;

    const segs = [];
    if (c.photoMins) {
      const seg = h("div", { class: "cycle-seg cycle-photos", style: { flexGrow: String(c.photoMins) }, title: `Photos: ${c.n} × ${fmtMinutes(c.each)} = ${fmtMinutes(c.photoMins)}` },
        h("span", { class: "cycle-label" }, h("span", { class: "cycle-name" }, "Photos")),
        h("span", { class: "cycle-min mono" }, `${c.n}${NB}×${NB}${fmtMinutes(c.each)}`));
      for (let k = 1; k < c.n && k < 60; k += 1) {
        seg.append(h("i", { class: "cycle-tick", style: { left: `${(k / c.n) * 100}%` } }));
      }
      segs.push(seg);
    }
    for (const { t, i } of c.cards) {
      const dur = Number(t.minutes);
      const name = t.name || t.app_id;
      segs.push(h("div", { class: "cycle-seg", style: { flexGrow: String(dur) }, title: `${String(i + 1).padStart(2, "0")} ${name}: ${fmtMinutes(dur)}` },
        h("span", { class: "cycle-label" }, h("span", { class: "cycle-num mono" }, String(i + 1).padStart(2, "0")), h("span", { class: "cycle-name" }, name)),
        h("span", { class: "cycle-min mono" }, fmtMinutes(dur))));
    }
    setChildren(timeline, segs);
    timeline.hidden = !segs.length;
    labelTimeline();
  }

  // --- fields ---------------------------------------------------------------------------------

  function numberField({ value, min, max, step = 1, label, suffix, onchange, path, cls = "" }) {
    const input = h("input", { type: "number", inputmode: "numeric", class: `num ${cls}`.trim(), min, max, step, value, "aria-label": label });
    input.addEventListener("input", () => {
      clearFieldError(input);
      const v = input.value === "" ? NaN : Number(input.value);
      onchange(Number.isFinite(v) ? v : input.value);
      changed();
    });
    if (path) {
      fields.set(path, input);
      fields.set(path.replace(/^rotation\./, ""), input);
    }
    return h("label", { class: "inline-field" }, input, suffix ? h("span", { class: "inline-suffix" }, suffix) : null);
  }

  // --- the photo stream -------------------------------------------------------------------------

  function photosSection() {
    const p = draft.photos;
    const help = h("p", { class: "section-hint form-summary" });
    const updateHelp = () => {
      const n = Number(p.in_a_row);
      const each = Number(p.minutes_each);
      if (!p.enabled) help.textContent = "Off: the cycle is the cards below only.";
      else if (n > 0 && each > 0) help.textContent = `${n} photo${n === 1 ? "" : "s"} back to back, ${fmtMinutes(each)} each, then every card below once, then photos again.`;
      else help.textContent = "Photos back to back, then every card below once, then photos again.";
    };
    const enabled = switchInput({ checked: p.enabled, label: "Show photos", id: "photos-enabled", onchange: (v) => { p.enabled = v; block.classList.toggle("off", !v); updateHelp(); changed(); } });
    const inARow = numberField({ value: p.in_a_row, min: 1, max: MAX_IN_A_ROW, label: "Photos in a row", path: "rotation.photos.in_a_row", onchange: (v) => { p.in_a_row = v; updateHelp(); } });
    const each = numberField({ value: p.minutes_each, min: 1, max: MAX_MINUTES, label: "Minutes per photo", suffix: "min", path: "rotation.photos.minutes_each", onchange: (v) => { p.minutes_each = v; updateHelp(); } });
    const order = segmented({
      label: "Photo order", small: true, value: p.order,
      options: [{ value: "shuffle", label: "Shuffle" }, { value: "sequential", label: "In order" }],
      onchange: (v) => { p.order = v; clearFieldError(order.el); changed(); },
    });
    fields.set("rotation.photos.order", order.el);
    fields.set("photos.order", order.el);
    fields.set("rotation.photos.enabled", enabled);
    fields.set("photos.enabled", enabled);
    updateHelp();
    const block = h("div", { class: `form-block${p.enabled ? "" : " off"}` },
      h("div", { class: "row-field" }, h("label", { for: "photos-enabled" }, "Show photos"), enabled),
      h("div", { class: "row-field" }, h("label", null, "Photos in a row", h("span", { class: "field-help" }, `How many photos show before the cards (1 to ${MAX_IN_A_ROW}).`)), inARow),
      fieldError(inARow.firstChild),
      h("div", { class: "row-field" }, h("label", null, "Minutes each", h("span", { class: "field-help" }, "A refresh takes about 19 s; the refresh floor below can stretch this.")), each),
      fieldError(each.firstChild),
      h("div", { class: "row-field" }, h("span", { class: "field-label" }, "Order"), order.el),
      fieldError(order.el),
      help);
    fields.set("rotation.photos", block);
    fields.set("photos", block);
    return h("section", { class: "section rotation-photos" }, eyebrow("Photos"), block);
  }

  // --- the cards -----------------------------------------------------------------------------

  const list = h("ol", { class: "slots" });

  function renderCards(focusSel) {
    fields.forEach((_, k) => {
      if (k.startsWith("rotation.apps[") || k.startsWith("apps[")) fields.delete(k);
    });
    list.replaceChildren(...draft.apps.map((t, i) => cardRow(t, i)));
    if (focusSel) {
      const target = list.querySelector(focusSel);
      if (target) target.focus();
    }
    updateCycle();
  }

  function move(from, to, focusSel) {
    if (to < 0 || to >= draft.apps.length || from === to) return;
    const [t] = draft.apps.splice(from, 1);
    draft.apps.splice(to, 0, t);
    renderCards(focusSel && focusSel(to));
    changed();
  }

  function cardRow(t, i) {
    const n = draft.apps.length;
    const name = t.name || t.app_id;
    const handle = h("button", { class: "handle", type: "button", "aria-label": `Reorder ${name}: drag, or use the arrow keys`, title: "Drag to reorder" }, icon("grip"));
    const toggle = switchInput({ checked: t.enabled, label: `Show ${name} in the cycle`, onchange: (v) => { t.enabled = v; row.classList.toggle("off", !v); changed(); } });
    const up = h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Move ${name} up`, title: "Move up", disabled: i === 0, dataset: { act: "up" }, onclick: () => move(i, i - 1, (to) => `li:nth-child(${to + 1}) [data-act="${to === 0 ? "down" : "up"}"]`) }, icon("up"));
    const down = h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Move ${name} down`, title: "Move down", disabled: i === n - 1, dataset: { act: "down" }, onclick: () => move(i, i + 1, (to) => `li:nth-child(${to + 1}) [data-act="${to === n - 1 ? "up" : "down"}"]`) }, icon("down"));

    const fieldsRow = h("div", { class: "slot-fields" },
      h("span", { class: "field-label" }, "Show for"),
      numberField({ value: t.minutes, min: 1, max: MAX_MINUTES, label: `Minutes to show ${name}`, suffix: "min", path: `rotation.apps[${i}].minutes`, onchange: (v) => { t.minutes = v; } }),
      h("div", { class: "slot-move" }, up, down));
    const row = h("li", { class: `slot${t.enabled ? "" : " off"}`, dataset: { index: String(i) } },
      handle,
      h("div", { class: "slot-title" }, h("span", { class: "slot-num mono" }, String(i + 1).padStart(2, "0")), h("span", { class: "slot-name" }, name)),
      h("div", { class: "slot-toggle" }, toggle),
      h("div", { class: "slot-main" },
        t.description ? h("p", { class: "slot-desc" }, t.description) : null,
        fieldsRow,
        h("p", { class: "field-error", hidden: true })));
    for (const path of [`rotation.apps[${i}]`, `apps[${i}]`, `rotation.apps[${i}].app_id`, `apps[${i}].app_id`, `rotation.apps[${i}].enabled`, `apps[${i}].enabled`]) fields.set(path, row);
    wireDrag(handle, row);
    handle.addEventListener("keydown", (e) => {
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        const to = e.key === "ArrowUp" ? i - 1 : i + 1;
        move(i, to, (t2) => `li:nth-child(${t2 + 1}) .handle`);
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
      handle.focus(); // preventDefault would otherwise leave the handle unfocused after a click
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
        const before = draft.apps;
        draft.apps = order.map((idx) => before[idx]);
        renderCards();
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
    const summaryLine = h("p", { class: "section-hint" });
    const updateSummary = () => {
      summaryLine.textContent = q.enabled
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
    summaryLine.classList.add("form-summary");
    const block = h("div", { class: `form-block${q.enabled ? "" : " off"}` },
      h("div", { class: "row-field" }, h("label", { for: "quiet-enabled" }, "Quiet hours"), enabled),
      h("div", { class: "row-field row-field-tight" }, h("span", { class: "field-label" }, "From"), h("div", { class: "time-range" }, start, h("span", { class: "muted" }, "to"), end)),
      fieldError(start), fieldError(end),
      h("div", { class: "row-field row-field-stack" }, h("span", { class: "field-label" }, "Starting on"), chips),
      fieldError(chips),
      summaryLine);
    return h("section", { class: "section" }, eyebrow("Quiet hours"), block);
  }

  function panelSection() {
    const p = draft.panel;
    // both rows: a label with its help, then a short number and a one-letter
    // unit, so they line up (the floor's minutes go in its help text)
    const floorHelp = h("span", { class: "field-help" });
    const updateHints = () => {
      const f = Number(p.refresh_floor_seconds);
      const asMinutes = Number.isFinite(f) && f >= 60 ? ` (now ${fmtMinutes(Math.floor(f / 60))}${f % 60 ? ` ${f % 60} s` : ""})` : "";
      floorHelp.textContent = `The least time between scheduled refreshes${asMinutes}. The panel likes at least 180 s.`;
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
    const rowEl = target.closest && target.closest(".slot");
    const p = errorSlots.get(target) || (rowEl && rowEl.querySelector(".field-error"));
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
    const named = m ? m[1] : "";
    let best = "";
    for (const path of fields.keys()) {
      if ((named === path || named.startsWith(`${path}.`) || named.startsWith(`${path}[`) || named.startsWith(`${path}:`)) && path.length > best.length) best = path;
    }
    // "rotation.apps[0].minutes must be ..." reads better as "Must be ..."
    // under the field itself; other messages are shown as they are
    let human = message;
    if (m && /^(:\s*|\s+must\b|\s+has\b|\s+is\b)/.test(message.slice(m[1].length))) {
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
    const rowEl = target.closest(".slot");
    const p = errorSlots.get(target) || (rowEl && rowEl.querySelector(".field-error"));
    if (p) {
      p.textContent = message;
      p.hidden = false;
    }
    target.scrollIntoView({ block: "center", behavior: "smooth" });
    if (typeof target.focus === "function" && target.tagName !== "LI" && target.tagName !== "DIV") target.focus({ preventScroll: true });
  }

  // --- save / load ------------------------------------------------------------------------------

  /** The error path of the first number field left blank, or "". */
  function blankNumber() {
    for (const [path, el] of fields) {
      if (el.tagName === "INPUT" && el.type === "number" && el.value === "") return path;
    }
    return "";
  }

  async function save() {
    if (saving) return;
    clearErrors();
    const blank = blankNumber();
    if (blank) {
      showError(`${blank}: enter a number`); // a blank would only come back as a 422 about ''
      return;
    }
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
      // remember what is saved already, in case the rotation is refused
      server = { ...server, settings };
      const rotation = await api("/api/rotation", { method: "PUT", quiet: true, body: { photos: photosBody(draft.photos), apps: draft.apps.map(turnBody) } });
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
    const photos = photosSection();
    renderCards();
    body.replaceChildren(
      h("div", { class: "rotation-main" },
        h("section", { class: "section rotation-cycle" }, eyebrow("One cycle", total), timeline, summary),
        photos,
        h("section", { class: "section rotation-slots" }, eyebrow("Cards"), h("p", { class: "section-hint" }, "Every card that is on shows once per cycle, in this order. Drag the handle, or use the arrows, to change the order."), list)),
      h("div", { class: "rotation-side" }, quietSection(), panelSection()));
    changed();
  }

  Promise.all([api("/api/rotation"), api("/api/settings")])
    .then(([rotation, settings]) => {
      if (destroyed) return;
      server = { rotation, settings };
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

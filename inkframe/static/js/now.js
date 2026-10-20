// Now: the frame's current image, what is next, and the panel's state.
// Polls /api/status every 5 s while the tab is visible; countdowns tick
// locally every second in between.

import { api, queuedMessage, serverNow } from "./api.js";
import { displayName, eyebrow, fmtAgo, fmtAt, fmtClock, fmtCountdown, fmtHM, h, icon, pageHead, parseTime, setChildren, swatchStrip, toast } from "./ui.js";

const POLL_MS = 5000;
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function describeDays(days) {
  const set = [...new Set(days)].sort();
  if (set.length === 7) return "every day";
  if (set.length === 0) return "no days";
  if (set.join() === "0,1,2,3,4") return "weekdays";
  if (set.join() === "5,6") return "weekends";
  return set.map((d) => DAY_NAMES[d]).join(", ");
}

const STATE_TEXT = { asleep: "Asleep", awake: "Awake", refreshing: "Refreshing" };

/** What a refresh showed, in words ("Palette test card" for the forced layout). */
function sourceName(src) {
  if (!src) return "Nothing yet";
  if (src.type === "app" && src.id === "patterns" && src.variant === 2) return "Palette test card";
  return displayName(src.label);
}

const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

/** Set a large label; long ones step down a size instead of breaking mid-word. */
function setTitle(el, text) {
  el.textContent = text;
  el.classList.toggle("now-title-long", text.length > 14);
  el.title = text.length > 14 ? text : "";
}

export function nowView(root) {
  let status = null;
  let destroyed = false;
  let pollTimer = 0;
  let inflight = false;
  let again = false;
  let frameKey = null;
  let failures = 0;
  let fastPolled = null;

  // --- skeleton -----------------------------------------------------------------------
  const stateChip = h("span", { class: "state-chip", "data-state": "unknown", role: "status" }, h("i", { class: "state-dot" }), h("span", { class: "state-text" }, "Connecting"));
  const head = pageHead("Now", "What the frame shows, and what comes next");

  const frameImg = h("img", { class: "frame-img", width: 600, height: 400, alt: "The image on the panel", hidden: true });
  const frameEmpty = h("div", { class: "frame-empty" }, swatchStrip("swatches-lg"), h("p", null, "Nothing on the panel yet"), h("p", { class: "muted" }, "The first refresh draws here."));
  const refreshBar = h("div", { class: "refresh-bar", "aria-hidden": "true" }, ["black", "white", "yellow", "red", "blue", "green"].map((c) => h("i", { class: `sw-${c}` })));
  const caption = h("span", { class: "frame-cap mono" }, "600 × 400");
  const figure = h("figure", { class: "frame" }, h("div", { class: "bezel" }, h("div", { class: "screen" }, frameImg, frameEmpty, refreshBar)), h("figcaption", { class: "frame-foot" }, caption, stateChip));

  const showingLabel = h("p", { class: "now-title" }, "—");
  const showingMeta = h("p", { class: "now-meta" });
  const showingLink = h("a", { class: "link", hidden: true }, "Open in the editor");
  const showing = h("section", { class: "now-block" }, eyebrow("On the panel"), showingLabel, showingMeta, showingLink);

  // Up next shows a queued show-now or show-next first (it fires before the
  // rotation's own next item), then quiet hours, then the plain rotation.
  // The state badge sits in the meta line, which wraps; never in the eyebrow.
  const nextLabel = h("p", { class: "now-title now-title-sm" }, "—");
  const countdown = h("span", { class: "countdown mono" });
  const nextBadge = h("span", { class: "badge badge-warn", hidden: true });
  const nextText = h("span");
  const nextMeta = h("p", { class: "now-meta" }, nextBadge, nextText);
  const progress = h("div", { class: "progress", role: "progressbar", "aria-label": "Time until the next refresh", "aria-valuemin": "0", "aria-valuemax": "100" }, h("i"));
  const next = h("section", { class: "now-block" }, eyebrow("Up next"), h("div", { class: "next-row" }, nextLabel, countdown), nextMeta, progress);

  const nextBtn = h("button", { class: "btn btn-primary", type: "button", onclick: showNext }, icon("next"), h("span", null, "Show next"));
  const pauseBtn = h("button", { class: "btn", type: "button", onclick: togglePause }, icon("pause"), h("span", null, "Pause rotation"));
  const actions = h("div", { class: "now-actions" }, nextBtn, pauseBtn);

  const fact = (term) => {
    const dd = h("dd");
    return { row: h("div", null, h("dt", null, term), dd), dd };
  };
  const fPanel = fact("Panel");
  const fRefresh = fact("Last refresh");
  const fKeep = fact("Keep-alive");
  const fQuiet = fact("Quiet hours");
  const fFloor = fact("Refresh floor");
  const facts = h("dl", { class: "facts" }, fPanel.row, fRefresh.row, fKeep.row, fQuiet.row, fFloor.row);

  const errorNote = h("div", { class: "note note-error", hidden: true, role: "alert" });
  const offline = h("div", { class: "note note-warn", hidden: true }, "Cannot reach the frame right now; retrying every few seconds.");

  const side = h("div", { class: "now-side" }, offline, h("div", { class: "now-pair" }, showing, next), actions, h("section", { class: "now-block" }, eyebrow("Status"), facts), errorNote);
  root.append(head, h("div", { class: "now-grid" }, figure, side));

  // --- rendering ----------------------------------------------------------------------

  function loadFrame(key) {
    const url = `/api/display/frame.png?v=${encodeURIComponent(key)}`;
    const probe = new Image();
    probe.onload = () => {
      if (destroyed) return;
      frameImg.src = url;
      frameImg.hidden = false;
      frameEmpty.hidden = true;
    };
    probe.onerror = () => {
      if (destroyed) return;
      if (frameImg.hidden) frameEmpty.hidden = false;
    };
    probe.src = url;
  }

  function render() {
    if (!status) return;
    const { panel, rotation, settings } = status;
    const nowMs = serverNow();

    // panel state
    const state = panel.state in STATE_TEXT ? panel.state : "asleep";
    stateChip.dataset.state = state;
    stateChip.querySelector(".state-text").textContent = STATE_TEXT[state];
    figure.classList.toggle("refreshing", state === "refreshing");
    setChildren(
      fPanel.dd,
      h("span", { class: "state-inline", "data-state": state }, h("i", { class: "state-dot" }), STATE_TEXT[state]),
      state === "refreshing" ? h("span", { class: "muted" }, " · about 19 s") : null,
    );

    // the frame image follows the last refresh
    const key = panel.last_refresh_at || panel.last_attempt_at || "initial";
    if (key !== frameKey) {
      frameKey = key;
      loadFrame(key);
    }

    // what is showing
    const src = panel.last_source;
    setTitle(showingLabel, sourceName(src));
    showingLink.hidden = !(src && src.type === "photo" && src.id);
    if (src && src.type === "photo" && src.id) showingLink.href = `#/photos/${src.id}`;
    const when = panel.last_refresh_at;
    // phones drop "six colours" so the caption stays on one line beside the chip
    setChildren(caption, "600 × 400", h("span", { class: "cap-extra" }, " · six colours"), when ? ` · ${fmtClock(when, nowMs)}` : null);

    // pause button
    pauseBtn.replaceChildren(icon(rotation.paused ? "play" : "pause"), h("span", null, rotation.paused ? "Resume rotation" : "Pause rotation"));
    pauseBtn.className = rotation.paused ? "btn btn-primary" : "btn";
    nextBtn.className = rotation.paused ? "btn" : "btn btn-primary";

    // quiet hours, in the same clock style as every other time on the page
    const qh = settings.quiet_hours;
    setChildren(
      fQuiet.dd,
      qh.enabled ? `${fmtHM(qh.start)}–${fmtHM(qh.end)}, ${describeDays(qh.days)}` : "Off",
      rotation.quiet ? h("span", { class: "badge badge-warn" }, "now") : null,
    );
    const floor = settings.refresh_floor_seconds;
    setChildren(fFloor.dd, floor % 60 === 0 ? `${floor / 60} min` : `${floor} s`, h("span", { class: "muted" }, "between scheduled refreshes"));

    // error
    const err = panel.last_error;
    errorNote.hidden = !err;
    if (err) {
      errorNote.replaceChildren(
        h("p", { class: "note-title" }, icon("alert"), `Last error · ${fmtClock(err.at, nowMs)}`),
        h("p", { class: "mono note-msg" }, [err.kind, err.app_id].filter(Boolean).join(" · ") + (err.message ? `: ${err.message}` : "")),
      );
    }
    tick();
  }

  function setNext({ title, left = null, badge = "", text = "", link = null, at = null, from = null }) {
    setTitle(nextLabel, title);
    countdown.textContent = left === null ? "" : left > 1000 ? fmtCountdown(left) : "now";
    nextBadge.hidden = !badge;
    nextBadge.textContent = badge;
    setChildren(nextText, badge && text ? " " : null, text, link);
    progress.hidden = at === null || from === null || at <= from;
    if (!progress.hidden) {
      const frac = Math.min(1, Math.max(0, (serverNow() - from) / (at - from)));
      progress.firstChild.style.transform = `scaleX(${frac})`;
      progress.setAttribute("aria-valuenow", String(Math.round(frac * 100)));
    }
  }

  // the parts that change every second
  function tick() {
    if (!status) return;
    const { panel, rotation, settings } = status;
    const nowMs = serverNow();
    const from = parseTime(panel.last_refresh_at) ?? parseTime(rotation.current && rotation.current.started_at);
    const p = rotation.pending;
    let due = null; // the deadline being counted down

    if (p) {
      const at = parseTime(p.fires_at);
      const left = at - nowMs;
      due = p.fires_at;
      setNext({
        title: displayName(p.label),
        left,
        badge: "Queued",
        text: left > 1000 ? `${p.kind === "next" ? "Show next" : "Show now"}, ${fmtAt(p.fires_at, nowMs)}` : "Refreshing as soon as the panel allows",
        at,
        from,
      });
    } else if (rotation.paused) {
      setNext({ title: "Rotation paused", text: "Show next and show-now still work, and so does the keep-alive." });
    } else if (!rotation.next_at) {
      setNext({ title: "Nothing scheduled", text: "No slot is enabled. ", link: h("a", { class: "link", href: "#/rotation" }, "Edit the rotation") });
    } else {
      const at = parseTime(rotation.next_at);
      const left = at - nowMs;
      const title = rotation.next ? displayName(rotation.next.label) : "Next";
      due = rotation.next_at;
      if (left <= 0) setNext({ title, left, text: "Refreshing as soon as the panel allows", at, from });
      else if (rotation.quiet) setNext({ title, left, badge: "Quiet hours", text: `until ${fmtAt(rotation.quiet_until || rotation.next_at, nowMs).replace(/^at /, "")}`, at, from });
      else setNext({ title, left, text: capital(fmtAt(rotation.next_at, nowMs)), at, from });
    }
    // the moment has passed: look again soon (once per deadline, then the
    // normal 5 s poll; the floor or quiet hours may keep it waiting)
    if (due && parseTime(due) - nowMs < -2000 && fastPolled !== due) {
      fastPolled = due;
      schedulePoll(1500);
    }

    const ka = rotation.keepalive_at;
    setChildren(fKeep.dd, ka ? `in ${fmtCountdown(parseTime(ka) - nowMs)}` : "—", ka ? h("span", { class: "muted" }, `at least every ${settings.keepalive_hours} h`) : null);
    const when = panel.last_refresh_at;
    const whenText = when ? `${fmtClock(when, nowMs)} · ${fmtAgo(when, nowMs)}` : "";
    fRefresh.dd.textContent = when ? whenText : "never";
    const kind = panel.last_source && panel.last_source.type === "keepalive" ? "Keep-alive refresh" : "Refreshed";
    showingMeta.textContent = when ? `${kind} ${whenText}` : "The panel has not been refreshed yet.";
  }

  // --- polling ------------------------------------------------------------------------

  function schedulePoll(ms) {
    clearTimeout(pollTimer);
    if (destroyed || document.hidden) return;
    pollTimer = setTimeout(poll, ms);
  }

  async function poll() {
    if (destroyed) return;
    if (inflight) {
      again = true;
      return;
    }
    inflight = true;
    clearTimeout(pollTimer);
    try {
      status = await api("/api/status", { quiet: true });
      failures = 0;
      offline.hidden = true;
      side.classList.remove("stale");
      render();
    } catch {
      failures += 1;
      if (failures >= 2) {
        offline.hidden = false;
        side.classList.add("stale");
      }
    } finally {
      inflight = false;
      if (again) {
        again = false;
        poll();
      } else {
        schedulePoll(POLL_MS);
      }
    }
  }

  function applySnapshot(result) {
    if (status && result) {
      status = { ...status, panel: result.panel, rotation: result.rotation };
      render();
    }
  }

  async function showNext() {
    nextBtn.disabled = true;
    try {
      const result = await api("/api/display/next", { method: "POST" });
      applySnapshot(result);
      const label = result.rotation.pending ? displayName(result.rotation.pending.label) : "The next item";
      toast(queuedMessage(result, label), "ok");
    } catch {
      // toasted by api()
    } finally {
      nextBtn.disabled = false;
    }
  }

  async function togglePause() {
    const paused = status && status.rotation.paused;
    pauseBtn.disabled = true;
    try {
      const result = await api(`/api/display/${paused ? "resume" : "pause"}`, { method: "POST" });
      applySnapshot(result);
      toast(paused ? "Rotation resumed." : "Rotation paused. The panel keeps its image.", "ok");
    } catch {
      // toasted by api()
    } finally {
      pauseBtn.disabled = false;
    }
  }

  const onVisibility = () => {
    if (document.hidden) clearTimeout(pollTimer);
    else poll();
  };
  document.addEventListener("visibilitychange", onVisibility);
  const ticker = setInterval(tick, 1000);
  poll();

  return {
    destroy() {
      destroyed = true;
      clearTimeout(pollTimer);
      clearInterval(ticker);
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}

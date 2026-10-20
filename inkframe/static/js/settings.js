// Settings: the measured palette, display facts and rotation, storage,
// the log tail and restarting the service.

import { api, fetchImage, queuedMessage } from "./api.js";
import { confirmDialog, emptyState, eyebrow, fmtBytes, fmtDuration, h, icon, loading, pageHead, switchInput, toast } from "./ui.js";

const COLOURS = [
  { key: "black", name: "Black" },
  { key: "white", name: "White" },
  { key: "yellow", name: "Yellow" },
  { key: "red", name: "Red" },
  { key: "blue", name: "Blue" },
  { key: "green", name: "Green" },
];

const toHex = ([r, g, b]) => `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
function fromHex(text) {
  const m = String(text).trim().match(/^#?([0-9a-f]{6})$/i);
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const LOG_LEVELS = { D: "debug", I: "info", W: "warn", E: "error", C: "error" };

export function settingsView(root) {
  let destroyed = false;
  let settings = null;
  let status = null;
  let uptimeBase = null;
  let restartTimer = 0;
  let testCardUrl = "";

  const body = h("div", { class: "settings-body" }, loading("Loading settings"));
  root.append(pageHead("Settings", "The panel's colours, the display, storage and the service"), body);

  // --- palette ------------------------------------------------------------------------------

  let palette = null; // the draft {black: [r,g,b], ...}
  let useMeasured = true;
  const palDirty = () => JSON.stringify({ m: palette, u: useMeasured }) !== JSON.stringify({ m: settings.palette.measured, u: settings.palette.use_measured });

  function paletteSection() {
    palette = JSON.parse(JSON.stringify(settings.palette.measured));
    useMeasured = settings.palette.use_measured;
    const strip = h("div", { class: "pal-strip", "aria-hidden": "true" });
    const saveBtn = h("button", { class: "btn btn-primary", type: "button", disabled: true }, icon("check"), h("span", null, "Save palette"));
    const revertBtn = h("button", { class: "btn btn-quiet", type: "button", disabled: true }, "Revert");
    const rows = [];

    const sync = () => {
      strip.replaceChildren(...COLOURS.map((c) => {
        const [r, g, b] = palette[c.key];
        const light = 0.2126 * r + 0.7152 * g + 0.0722 * b > 140;
        return h("span", { class: "pal-strip-cell", style: { background: toHex(palette[c.key]) } },
          h("span", { class: "pal-strip-name mono", style: { color: light ? "#18181a" : "#f4f3ee" } }, c.name));
      }));
      const dirty = palDirty();
      saveBtn.disabled = !dirty;
      revertBtn.disabled = !dirty;
    };

    for (const c of COLOURS) {
      const rgb = h("span", { class: "pal-rgb mono" });
      const colour = h("input", { type: "color", class: "pal-picker", "aria-label": `${c.name}: pick the measured colour` });
      const swatch = h("label", { class: "pal-swatch" }, colour);
      const hex = h("input", { type: "text", class: "input mono pal-hex", maxlength: 7, spellcheck: "false", autocomplete: "off", "aria-label": `${c.name}: hex value` });
      const show = (value, { fromHexInput = false } = {}) => {
        const hx = toHex(value);
        swatch.style.background = hx;
        colour.value = hx;
        if (!fromHexInput) hex.value = hx;
        rgb.textContent = `rgb ${value.join(" ")}`;
      };
      colour.addEventListener("input", () => {
        palette[c.key] = fromHex(colour.value);
        show(palette[c.key]);
        sync();
      });
      // half-typed values are not errors: red only once six digits are in
      // (or something that is not a hex digit); blur puts back the last good one
      hex.addEventListener("input", () => {
        const v = fromHex(hex.value);
        const raw = hex.value.trim().replace(/^#/, "");
        hex.classList.toggle("invalid", !v && (raw.length >= 6 || /[^0-9a-f]/i.test(raw)));
        if (!v) return;
        palette[c.key] = v;
        show(v, { fromHexInput: true });
        sync();
      });
      hex.addEventListener("blur", () => {
        hex.classList.remove("invalid");
        show(palette[c.key]);
      });
      show(palette[c.key]);
      rows.push({ key: c.key, show });
      rows[rows.length - 1].el = h("div", { class: "pal-row" }, swatch, h("div", { class: "pal-name" }, h("span", null, c.name), rgb), hex);
    }

    const measured = switchInput({ checked: useMeasured, id: "use-measured", label: "Use the measured palette", onchange: (v) => { useMeasured = v; sync(); } });

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      try {
        settings = await api("/api/settings", { method: "PUT", body: { palette: { measured: palette, use_measured: useMeasured } } });
        sync();
        toast("Palette saved. Renders and previews use these colours from now on.", "ok");
        if (testCardUrl) previewCard();
      } catch {
        sync();
      }
    });
    revertBtn.addEventListener("click", () => {
      palette = JSON.parse(JSON.stringify(settings.palette.measured));
      useMeasured = settings.palette.use_measured;
      measured.checked = useMeasured;
      for (const r of rows) r.show(palette[r.key]);
      sync();
    });

    const cardImg = h("img", { class: "app-preview-img", width: 600, height: 400, alt: "The palette test card as the panel would show it" });
    const cardBezel = h("div", { class: "bezel test-card", hidden: true }, h("div", { class: "screen" }, cardImg));
    async function previewCard() {
      try {
        const url = await fetchImage("/api/apps/patterns/preview.png?variant=2");
        if (testCardUrl) URL.revokeObjectURL(testCardUrl);
        testCardUrl = url;
        cardImg.src = url;
        cardBezel.hidden = false;
      } catch (e) {
        toast(`Test card preview: ${e.message}`, "error");
      }
    }
    const showCard = h("button", { class: "btn", type: "button", onclick: async () => {
      try {
        const result = await api("/api/display/show", { method: "POST", body: { app_id: "patterns", variant: 2 } });
        toast(queuedMessage(result, "The palette test card"), "ok");
      } catch {
        // toasted
      }
    } }, icon("show"), h("span", null, "Show test card on panel"));
    const previewBtn = h("button", { class: "btn", type: "button", onclick: previewCard }, "Preview here");

    sync();
    return h("section", { class: "section" }, eyebrow("Palette"),
      h("p", { class: "section-hint" }, "The colours this panel really shows. Put the test card on the panel, photograph it in daylight, sample each patch and enter it here. Previews then match the panel, and dithering aims at these colours."),
      strip,
      h("div", { class: "pal-grid" }, rows.map((r) => r.el)),
      h("div", { class: "row-field" }, h("label", { for: "use-measured" }, "Use the measured palette", h("span", { class: "field-help" }, "Off: dither against the nominal primaries instead.")), measured),
      h("div", { class: "button-row" }, saveBtn, revertBtn),
      h("div", { class: "subsection" },
        h("h3", { class: "subsection-title" }, "Test card"),
        h("p", { class: "section-hint" }, "Six labelled patches and an arrow that marks up. It shows once and leaves your Patterns settings alone."),
        h("div", { class: "button-row" }, showCard, previewBtn),
        cardBezel));
  }

  // --- display --------------------------------------------------------------------------------

  function displaySection() {
    const kind = status.display.kind;
    const env = status.system.display_env;
    const rotation = h("select", { class: "select", id: "panel-rotation" },
      h("option", { value: "90", selected: settings.display.rotation === 90 }, "90° (the driver's orientation)"),
      h("option", { value: "270", selected: settings.display.rotation === 270 }, "270° (mounted the other way up)"));
    rotation.addEventListener("change", async () => {
      const value = Number(rotation.value);
      try {
        settings = await api("/api/settings", { method: "PUT", body: { display: { rotation: value } } });
        toast(`Panel rotation set to ${value}°. It applies from the next refresh.`, "ok");
      } catch {
        rotation.value = String(settings.display.rotation);
      }
    });
    const what = kind === "mock"
      ? "Frames go to last_frame.png in the data folder; no panel is driven."
      : "Waveshare 4inch e-Paper HAT+ (E) over SPI.";
    return h("section", { class: "section" }, eyebrow("Display"),
      h("dl", { class: "facts" },
        h("div", null, h("dt", null, "Driver"), h("dd", null, h("span", { class: "badge badge-muted mono" }, kind), " ", what)),
        h("div", null, h("dt", null, "Panel"), h("dd", null, `${status.display.width} × ${status.display.height}, six colours, about 19 s per refresh`)),
        h("div", null, h("dt", null, "Set by"), h("dd", { class: "mono" }, `INKFRAME_DISPLAY=${env}`))),
      h("div", { class: "row-field" },
        h("label", { for: "panel-rotation" }, "Panel rotation", h("span", { class: "field-help" }, "Which way is up. If the test card's arrow points down, the frame hangs the other way round: switch to the other setting.")),
        rotation));
  }

  // --- storage --------------------------------------------------------------------------------

  // One bar split by what Inkframe keeps (its share of Inkframe's own data,
  // not of the disk, so a full-looking bar never reads as "disk full"), a
  // keyed row per kind, and the free space as plain text.
  function storageSection() {
    const st = status.system.storage || {};
    const items = [
      { key: "masters", label: "Photos", hint: "the originals", bytes: st.masters_bytes || 0 },
      { key: "renders", label: "Renders", hint: "dithered", bytes: st.renders_bytes || 0 },
      { key: "cache", label: "App caches", hint: "weather, news", bytes: st.cache_bytes || 0 },
      { key: "db", label: "Database", hint: "settings", bytes: st.db_bytes || 0 },
    ];
    const used = items.reduce((a, i) => a + i.bytes, 0);
    const pct = (b) => (used ? Math.round((b / used) * 100) : 0);
    return h("section", { class: "section" }, eyebrow("Storage", h("span", { class: "mono eyebrow-note" }, `${fmtBytes(used)} in use`)),
      h("div", { class: "storage-split", role: "img", "aria-label": items.map((i) => `${i.label} ${pct(i.bytes)}%`).join(", ") },
        items.filter((i) => i.bytes > 0).map((i) => h("i", { class: `st-${i.key}`, style: { flexGrow: String(i.bytes) } }))),
      h("div", { class: "storage" },
        items.map((i) => h("div", { class: "storage-row" },
          h("span", { class: `storage-key st-${i.key}`, "aria-hidden": "true" }),
          h("span", { class: "storage-label" }, i.label, h("span", { class: "muted" }, ` ${i.hint}`)),
          h("span", { class: "storage-pct mono" }, `${pct(i.bytes)}%`),
          h("span", { class: "storage-size mono" }, fmtBytes(i.bytes)))),
        h("div", { class: "storage-row storage-free" },
          h("span", { class: "storage-key", "aria-hidden": "true" }),
          h("span", { class: "storage-label" }, "Free on the disk"),
          h("span", { class: "storage-pct" }),
          h("span", { class: "storage-size mono" }, fmtBytes(status.system.disk_free_bytes)))),
      h("p", { class: "section-hint mono storage-path" }, status.system.data_dir));
  }

  // --- log --------------------------------------------------------------------------------------

  const logBox = h("div", { class: "log", tabindex: "0", role: "log", "aria-label": "Service log" });
  async function loadLog() {
    try {
      const { lines } = await api("/api/system/logs?n=200");
      const atBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 24;
      logBox.replaceChildren(...lines.map((line) => {
        const m = line.match(/^(\S+ \S+) ([DIWEC]) ([^:]+): ([\s\S]*)$/);
        if (!m) return h("div", { class: "log-line" }, line);
        const level = LOG_LEVELS[m[2]] || "info";
        return h("div", { class: `log-line log-${level}` },
          h("span", { class: "log-time" }, m[1].slice(11, 19)), " ",
          h("span", { class: "log-level" }, m[2]), " ",
          h("span", { class: "log-src" }, m[3]), " ",
          h("span", { class: "log-msg" }, m[4]));
      }));
      if (!lines.length) logBox.append(h("div", { class: "log-line muted" }, "Nothing logged yet."));
      if (atBottom || !logBox.dataset.loaded) logBox.scrollTop = logBox.scrollHeight;
      logBox.dataset.loaded = "1";
    } catch {
      // toasted
    }
  }

  function logSection() {
    const refresh = h("button", { class: "btn btn-sm", type: "button", onclick: loadLog }, icon("refresh"), h("span", null, "Refresh"));
    loadLog();
    return h("section", { class: "section" }, eyebrow("Log", refresh), logBox);
  }

  // --- service ----------------------------------------------------------------------------------

  const uptime = h("dd", { class: "mono" });
  const restartNote = h("div", { class: "note note-warn", hidden: true, role: "status" });

  function serviceSection() {
    const restartBtn = h("button", { class: "btn btn-danger", type: "button", onclick: restart }, icon("refresh"), h("span", null, "Restart service"));
    return h("section", { class: "section" }, eyebrow("Service"),
      h("dl", { class: "facts" },
        h("div", null, h("dt", null, "Version"), h("dd", { class: "mono" }, `Inkframe ${status.version}`)),
        h("div", null, h("dt", null, "Host"), h("dd", { class: "mono" }, status.system.hostname)),
        h("div", null, h("dt", null, "Uptime"), uptime)),
      h("div", { class: "button-row" }, restartBtn, h("span", { class: "field-help" }, "Stops the service; systemd starts it again within a few seconds.")),
      restartNote);
  }

  function tickUptime() {
    if (!uptimeBase) return;
    uptime.textContent = fmtDuration(uptimeBase.seconds + (performance.now() - uptimeBase.at) / 1000);
  }

  async function restart() {
    const ok = await confirmDialog({
      title: "Restart the service?",
      message: "The frame's web page and rotation stop for a few seconds and come back on their own. A panel refresh in progress finishes first.",
      confirm: "Restart",
      danger: true,
    });
    if (!ok) return;
    const before = uptimeBase ? uptimeBase.seconds + (performance.now() - uptimeBase.at) / 1000 : null;
    try {
      await api("/api/system/restart", { method: "POST" });
    } catch {
      return;
    }
    restartNote.hidden = false;
    restartNote.textContent = "Restarting… waiting for the frame to come back.";
    const t0 = performance.now();
    let sawDown = false;
    // back means: it failed to answer at least once, or it answers with a
    // smaller uptime than the old process would have by now
    const check = async () => {
      if (destroyed) return;
      const elapsed = (performance.now() - t0) / 1000;
      try {
        const s = await api("/api/status", { quiet: true });
        if (sawDown || (before !== null && s.system.uptime_seconds < before + elapsed - 2)) {
          restartNote.hidden = true;
          toast("Inkframe is back.", "ok");
          status = s;
          uptimeBase = { seconds: s.system.uptime_seconds, at: performance.now() };
          loadLog();
          return;
        }
      } catch {
        sawDown = true;
      }
      if (elapsed > 120) {
        restartNote.textContent = "The service has not come back after two minutes. On the Pi, look at journalctl -u inkframe; on a desktop, start it again by hand.";
        return;
      }
      restartTimer = setTimeout(check, 1500);
    };
    restartTimer = setTimeout(check, 1500);
  }

  // --- load -------------------------------------------------------------------------------------

  Promise.all([api("/api/settings"), api("/api/status")])
    .then(([s, st]) => {
      if (destroyed) return;
      settings = s;
      status = st;
      uptimeBase = { seconds: st.system.uptime_seconds, at: performance.now() };
      body.replaceChildren(
        h("div", { class: "settings-col" }, paletteSection(), displaySection()),
        h("div", { class: "settings-col" }, storageSection(), serviceSection(), logSection()));
      tickUptime();
    })
    .catch(() => {
      if (!destroyed) body.replaceChildren(emptyState("Could not load the settings", "The frame did not answer. Check that it is on the network, then reload."));
    });
  const ticker = setInterval(tickUptime, 1000);

  return {
    destroy() {
      destroyed = true;
      clearInterval(ticker);
      clearTimeout(restartTimer);
      if (testCardUrl) URL.revokeObjectURL(testCardUrl);
    },
    isDirty: () => !!(settings && palette) && palDirty(),
  };
}

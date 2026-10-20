// Apps: one card per app with a settings form generated from its
// settings_schema, a browser preview (never the panel) and "show now".

import { api, fetchImage, queuedMessage } from "./api.js";
import { emptyState, fieldMessage, h, icon, loading, pageHead, segmented, swatchStrip, switchInput, toast } from "./ui.js";

/** Build the input for one schema field; returns {el, input, read()}. */
function fieldControl(field, value, id, onchange) {
  const common = { id, name: field.key, "aria-describedby": field.help ? `${id}-help ${id}-error` : `${id}-error` };
  let input;
  let read;
  switch (field.type) {
    case "textarea":
      input = h("textarea", { ...common, class: "textarea", rows: 4, placeholder: field.placeholder || null, value: value ?? "" });
      read = () => input.value;
      break;
    case "list":
      // one entry per line, never soft-wrapped (a long URL scrolls sideways
      // instead of looking like two entries); the box grows with the list
      input = h("textarea", { ...common, class: "textarea mono textarea-list", wrap: "off", rows: listRows(value || []), placeholder: field.placeholder || "One per line", spellcheck: "false", autocapitalize: "off", autocomplete: "off", value: (value || []).join("\n") });
      input.addEventListener("input", () => {
        input.rows = listRows(input.value.split("\n"));
      });
      read = () => input.value.split("\n").map((s) => s.trim()).filter(Boolean);
      break;
    case "number":
      input = h("input", { ...common, type: "number", class: "num num-wide", inputmode: field.step === 1 ? "numeric" : "decimal", min: field.min ?? null, max: field.max ?? null, step: field.step ?? "any", value: value ?? "" });
      read = () => (input.value === "" ? null : Number(input.value));
      break;
    case "bool":
      input = switchInput({ checked: !!value, id, label: field.label });
      read = () => input.checked;
      break;
    case "select":
      input = h("select", { ...common, class: "select" }, (field.options || []).map((o) => h("option", { value: o.value, selected: o.value === value }, o.label)));
      read = () => input.value;
      break;
    default:
      input = h("input", { ...common, type: "text", class: "input", placeholder: field.placeholder || null, value: value ?? "", autocomplete: "off" });
      read = () => input.value;
  }
  input.addEventListener("input", onchange);
  input.addEventListener("change", onchange);
  return { input, read };
}

function listRows(lines) {
  return Math.min(10, Math.max(3, lines.length + 1));
}

/**
 * Names for the preview's layout picker.  When the app's own settings have a
 * layout select whose fixed choices match its variants (Weather: Now, Five
 * days, Poster, plus Rotate), the picker reuses those names; otherwise the
 * layouts are numbered.
 */
function variantNames(app) {
  const field = app.settings_schema.find((f) => f.type === "select" && /layout/i.test(`${f.key} ${f.label}`));
  if (field) {
    const fixed = (field.options || []).filter((o) => !/^(rotate|auto|random|cycle)$/i.test(o.value));
    if (fixed.length === app.variants) return fixed.map((o) => String(o.label).split(":")[0].trim());
  }
  return Array.from({ length: app.variants }, (_, i) => String(i + 1));
}

function needsAttention(app) {
  return app.settings_schema.filter((f) => f.required && (app.settings[f.key] === "" || app.settings[f.key] == null || (Array.isArray(app.settings[f.key]) && !app.settings[f.key].length)));
}

function appCard(app) {
  let savedValues = { ...app.settings };
  let variant = null; // null = let the app choose, like the rotation does
  let seed = null;
  let previewUrl = "";
  let previewing = false;
  const base = `app-${app.id}`;

  // --- form -----------------------------------------------------------------------------
  const controls = new Map();
  const errors = new Map();
  const formStatus = h("span", { class: "form-status", role: "status" });
  const saveBtn = h("button", { class: "btn btn-primary", type: "submit", disabled: true }, icon("check"), h("span", null, "Save"));
  const attention = h("span", { class: "badge badge-warn", hidden: true });

  const values = () => Object.fromEntries([...controls].map(([k, c]) => [k, c.read()]));
  const dirty = () => JSON.stringify(values()) !== JSON.stringify(Object.fromEntries(app.settings_schema.map((f) => [f.key, savedValues[f.key]])));
  const onchange = () => {
    saveBtn.disabled = !dirty();
    formStatus.classList.remove("error");
    formStatus.textContent = dirty() ? "Unsaved changes · the preview shows the saved settings" : "";
    for (const p of errors.values()) p.hidden = true;
    for (const c of controls.values()) c.input.classList.remove("invalid");
  };

  const fieldEls = app.settings_schema.map((f) => {
    const id = `${base}-${f.key}`;
    const control = fieldControl(f, app.settings[f.key], id, onchange);
    controls.set(f.key, control);
    const err = h("p", { class: "field-error", id: `${id}-error`, hidden: true });
    errors.set(f.key, err);
    const help = [f.help, f.type === "list" && !/per line/i.test(f.help || "") ? "One per line." : ""].filter(Boolean).join(" ");
    const label = h("label", { for: id, class: "field-label" }, f.label, f.required ? h("span", { class: "req", title: "Required" }, " *") : null);
    if (f.type === "bool") {
      return h("div", { class: "field field-bool" }, h("div", { class: "row-field" }, h("div", null, label, help ? h("p", { class: "field-help", id: `${id}-help` }, help) : null), control.input), err);
    }
    return h("div", { class: "field" }, label, control.input, help ? h("p", { class: "field-help", id: `${id}-help` }, help) : null, err);
  });

  const form = h("form", { class: "app-form", novalidate: true },
    fieldEls.length ? fieldEls : h("p", { class: "section-hint" }, app.id === "photos" ? "Nothing to set here: photos are managed on the Photos page, and their timing on the Rotation page." : "This app has no settings."),
    fieldEls.length ? h("div", { class: "form-actions" }, saveBtn, formStatus) : null);

  function refreshAttention() {
    const missing = needsAttention({ ...app, settings: savedValues });
    attention.hidden = !missing.length;
    attention.textContent = missing.length ? `Needs ${missing.map((f) => f.label.toLowerCase()).join(", ")}` : "";
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!dirty()) return;
    saveBtn.disabled = true;
    formStatus.textContent = "Saving…";
    try {
      const saved = await api(`/api/apps/${app.id}/settings`, { method: "PUT", body: values(), quiet: true });
      savedValues = saved;
      app.settings = saved;
      formStatus.textContent = "Saved";
      refreshAttention();
      toast(`${app.name} settings saved.`, "ok");
      if (previewUrl || previewing) preview();
    } catch (err) {
      formStatus.textContent = "";
      saveBtn.disabled = false;
      // "location: ..." names the field: the message goes under it, worded
      // for that place ("Must be at least 3"), as on the Rotation page;
      // anything else shows beside Save.  A network failure is also toasted.
      const key = [...controls.keys()].find((k) => err.message.startsWith(k) || err.message.includes(`${k}:`) || err.message.includes(`'${k}'`));
      if (key) {
        const p = errors.get(key);
        p.textContent = fieldMessage(err.message, key);
        p.hidden = false;
        controls.get(key).input.classList.add("invalid");
        controls.get(key).input.focus();
        formStatus.textContent = "Not saved: see the field above.";
      } else {
        formStatus.textContent = `Not saved: ${fieldMessage(err.message)}`;
      }
      formStatus.classList.add("error");
      if (!err.status) toast(err.message, "error");
    }
  });

  // --- preview ------------------------------------------------------------------------------
  const img = h("img", { class: "app-preview-img", width: 600, height: 400, alt: `${app.name} preview`, hidden: true });
  const placeholder = h("div", { class: "frame-empty" }, swatchStrip("swatches-lg"), h("p", null, "Preview"), h("p", { class: "muted" }, "Renders here, never on the panel."));
  const bezel = h("div", { class: "bezel app-bezel" }, h("div", { class: "screen" }, img, placeholder), h("div", { class: "stage-busy", "aria-hidden": "true" }, h("span", { class: "refresh-bar" }, ["black", "white", "yellow", "red", "blue", "green"].map((c) => h("i", { class: `sw-${c}` }))), h("span", { class: "stage-busy-label mono" }, "Rendering")));
  const cap = h("p", { class: "frame-cap mono" });
  const previewBtn = h("button", { class: "btn", type: "button", onclick: () => preview() }, icon("refresh"), h("span", null, "Preview"));
  const seedBtn = h("button", { class: "btn btn-quiet", type: "button", title: "Render again with a new random seed", onclick: () => { seed = Math.floor(Math.random() * 1e9); preview(); } }, icon("seed"), h("span", null, "New seed"));
  const showBtn = h("button", { class: "btn", type: "button", onclick: showNow }, icon("show"), h("span", null, "Show on panel now"));
  let variantSeg = null;
  const names = variantNames(app);
  if (app.variants > 1) {
    variantSeg = segmented({
      label: "Preview layout",
      small: true,
      value: "auto",
      options: [{ value: "auto", label: "Auto", title: "As the rotation would choose, with the saved settings" }, ...names.map((name, i) => ({ value: String(i), label: name, title: `Preview layout: ${name}` }))],
      onchange: (v) => { variant = v === "auto" ? null : Number(v); preview(); },
    });
  }

  async function preview() {
    previewing = true;
    bezel.classList.add("busy");
    previewBtn.disabled = true;
    const params = new URLSearchParams();
    if (variant !== null) params.set("variant", String(variant));
    if (seed !== null) params.set("seed", String(seed));
    const t0 = performance.now();
    try {
      const url = await fetchImage(`/api/apps/${app.id}/preview.png${params.toString() ? `?${params}` : ""}`);
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      previewUrl = url;
      img.src = url;
      img.hidden = false;
      placeholder.hidden = true;
      const ms = Math.round(performance.now() - t0);
      cap.textContent = ["600 × 400", variant === null ? "layout: auto" : `layout: ${names[variant] || variant + 1}`, seed !== null ? `seed ${seed}` : null, `${ms} ms`].filter(Boolean).join(" · ");
    } catch (err) {
      toast(`${app.name} preview: ${err.message}`, "error");
      cap.textContent = "The preview failed; see the message above.";
    } finally {
      previewing = false;
      bezel.classList.remove("busy");
      previewBtn.disabled = false;
    }
  }

  async function showNow() {
    showBtn.disabled = true;
    try {
      const result = await api("/api/display/show", { method: "POST", body: { app_id: app.id } });
      toast(queuedMessage(result, app.name), "ok");
    } catch {
      // toasted
    } finally {
      showBtn.disabled = false;
    }
  }

  refreshAttention();
  const card = h("article", { class: "app-card", id: base },
    h("header", { class: "app-head" },
      h("div", null, h("h2", { class: "app-name" }, app.name), h("p", { class: "app-desc" }, app.description)),
      attention),
    h("div", { class: "app-body" },
      h("div", { class: "app-settings" }, form),
      h("div", { class: "app-preview" },
        h("div", { class: "preview-controls" }, variantSeg ? h("div", { class: "preview-variant" }, h("span", { class: "field-label" }, "Preview layout"), variantSeg.el) : null, h("div", { class: "preview-buttons" }, previewBtn, seedBtn)),
        bezel, cap,
        h("div", { class: "preview-show" }, showBtn))));
  return {
    card,
    dirty,
    // the first preview happens by itself when the card scrolls into view
    previewOnce: () => {
      if (!previewUrl && !previewing) preview();
    },
    destroy: () => previewUrl && URL.revokeObjectURL(previewUrl),
  };
}

export function appsView(root) {
  let destroyed = false;
  let cards = [];
  let observer = null;
  const jump = h("nav", { class: "jump", "aria-label": "Apps on this page" });
  const body = h("div", { class: "apps-body" }, loading("Loading the apps"));
  root.append(pageHead("Apps", "Settings for each thing the frame can show. Previews render here, not on the panel."), jump, body);

  api("/api/apps")
    .then((apps) => {
      if (destroyed) return;
      if (!apps.length) {
        body.replaceChildren(emptyState("No apps loaded", "Check the service log on the Settings page."));
        return;
      }
      // photos has nothing to set, so it goes last
      const ordered = [...apps.filter((a) => a.id !== "photos"), ...apps.filter((a) => a.id === "photos")];
      cards = ordered.map(appCard);
      body.replaceChildren(...cards.map((c) => c.card));
      // previews render on the frame's one render thread, so they are asked
      // for one card at a time as the cards come into view
      if ("IntersectionObserver" in window) {
        observer = new IntersectionObserver((entries) => {
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            observer.unobserve(entry.target);
            const c = cards.find((x) => x.card === entry.target);
            if (c) c.previewOnce();
          }
        }, { threshold: 0.2 });
        for (const c of cards) observer.observe(c.card);
      }
      jump.replaceChildren(...ordered.map((a) => h("button", { class: "chip", type: "button", onclick: () => document.getElementById(`app-${a.id}`).scrollIntoView({ behavior: "smooth", block: "start" }) }, a.name)));
    })
    .catch(() => {
      if (!destroyed) body.replaceChildren(emptyState("Could not load the apps", "The frame did not answer. Check that it is on the network, then reload."));
    });

  return {
    destroy() {
      destroyed = true;
      if (observer) observer.disconnect();
      for (const c of cards) c.destroy();
    },
    isDirty: () => cards.some((c) => c.dirty()),
  };
}

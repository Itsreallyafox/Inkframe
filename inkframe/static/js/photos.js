// Photos: the grid of dithered renders, per-photo actions and the uploader.

import { api, getRecipeMeta, queuedMessage } from "./api.js";
import { confirmDialog, displayName, emptyState, fmtBytes, h, icon, loading, pageHead, setChildren, toast } from "./ui.js";
import { UploadQueue } from "./upload.js";

const IMAGE_EXT = /\.(jpe?g|png|gif|webp|bmp|tiff?|heic|heif|avif)$/i;

function isImage(file) {
  return (file.type && file.type.startsWith("image/")) || IMAGE_EXT.test(file.name || "");
}

const ERROR_TITLES = {
  408: "Timed out",
  413: "Too large",
  415: "Not a supported image",
  422: "Could not read it",
};

export function photosView(root) {
  let photos = [];
  let methodNames = {};
  let destroyed = false;

  const fileInput = h("input", { type: "file", accept: "image/*", multiple: true, class: "visually-hidden", tabindex: "-1", "aria-hidden": "true" });
  const addBtn = h("button", { class: "btn btn-primary", type: "button", onclick: () => fileInput.click() }, icon("upload"), h("span", null, "Add photos"));
  const head = pageHead("Photos", "", addBtn);
  const sub = head.querySelector(".page-sub") || h("p", { class: "page-sub" });
  if (!sub.parentNode) head.querySelector(".page-titles").append(sub);

  const dropzone = h(
    "div",
    { class: "dropzone", role: "button", tabindex: "0", "aria-label": "Add photos: choose files or drop them here" },
    icon("upload", "dropzone-icon"),
    h("div", null,
      h("p", { class: "dropzone-title" }, h("span", { class: "only-touch" }, "Tap to add photos"), h("span", { class: "only-pointer" }, "Drop photos here, or choose files")),
      h("p", { class: "dropzone-text" }, "JPEG, PNG or HEIC, up to 25 MB each. Landscape fills the frame; portrait gets white bars."),
    ),
  );
  const uploads = h("ul", { class: "uploads", "aria-label": "Uploads", "aria-live": "polite" });
  const grid = h("div", { class: "grid", role: "list" });
  const body = h("div", { class: "photos-body" }, loading("Loading photos"));
  root.append(head, fileInput, dropzone, uploads, body);

  // --- uploads ----------------------------------------------------------------------

  const rows = new Map();
  const queue = new UploadQueue({
    onChange: (job) => renderJob(job),
    onDone: (job) => {
      if (destroyed) return;
      photos.unshift(job.photo);
      renderGrid({ added: job.photo.id });
      setTimeout(() => {
        const r = rows.get(job.id);
        if (r) {
          r.li.classList.add("leaving");
          setTimeout(() => {
            r.li.remove();
            rows.delete(job.id);
          }, 250);
        }
      }, 2500);
    },
  });

  // one row per job, built once; progress events only update its parts so the
  // bar's transform transition stays smooth
  function jobRow(job) {
    const name = h("span", { class: "up-name", title: job.name }, job.name);
    const size = h("span", { class: "up-size mono" });
    const state = h("span", { class: "up-state mono" });
    const action = h("span", { class: "up-action" });
    const fill = h("i");
    const bar = h("div", { class: "up-bar", role: "progressbar", "aria-label": `Upload of ${job.name}`, "aria-valuemin": "0", "aria-valuemax": "100" }, fill);
    const note = h("p", { class: "up-error", hidden: true });
    const li = h("li", { class: "up-row" }, h("div", { class: "up-head" }, name, size, state, action), bar, note);
    return { li, size, state, action, fill, bar, note, actionKind: "" };
  }

  function renderJob(job) {
    if (destroyed) return;
    let r = rows.get(job.id);
    if (!r) {
      r = jobRow(job);
      rows.set(job.id, r);
      uploads.append(r.li);
    }
    r.li.dataset.state = job.state;
    const pct = Math.round((job.progress || 0) * 100);
    let state = "";
    if (job.state === "queued") state = "Waiting";
    else if (job.state === "preparing") state = "Preparing";
    else if (job.state === "uploading") state = `${pct}%`;
    else if (job.state === "rendering") state = "Rendering…";
    else if (job.state === "done") state = "Added";
    else if (job.state === "error") state = job.status === -1 ? "Cancelled" : ERROR_TITLES[job.status] || "Failed";
    r.state.textContent = state;
    r.size.textContent = job.bytes && job.converted ? `${fmtBytes(job.size)} → ${fmtBytes(job.bytes)}` : fmtBytes(job.size);
    const fraction = job.state === "uploading" ? job.progress : job.state === "rendering" || job.state === "done" ? 1 : 0;
    r.fill.style.transform = `scaleX(${fraction})`;
    r.bar.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
    // once the body has gone the frame finishes the photo whatever happens
    // here, so "rendering" offers no cancel (it would only hide the result)
    const cancellable = ["queued", "preparing", "uploading"].includes(job.state);
    const kind = cancellable ? "cancel" : job.state === "rendering" ? "wait" : job.state === "error" ? "dismiss" : "done";
    if (kind !== r.actionKind) {
      r.actionKind = kind;
      const removeRow = () => {
        r.li.remove();
        rows.delete(job.id);
      };
      setChildren(r.action,
        kind === "cancel" ? h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Cancel ${job.name}`, title: "Cancel", onclick: () => queue.cancel(job) }, icon("close")) : null,
        kind === "dismiss" ? h("button", { class: "btn-icon btn-quiet", type: "button", "aria-label": `Dismiss ${job.name}`, title: "Dismiss", onclick: removeRow }, icon("close")) : null,
        kind === "wait" ? h("span", { class: "up-done", title: "The frame is dithering it" }) : null,
        kind === "done" ? h("span", { class: "up-done" }, icon("check")) : null);
    }
    let note = "";
    if (job.state === "error" && job.status !== -1) note = errorNote(state, job.error);
    else if ((job.state === "uploading" || job.state === "rendering") && job.converted === false) note = "Sent as is: this browser cannot decode it, so the frame converts it.";
    r.note.hidden = !note;
    r.note.textContent = note;
    r.note.className = job.state === "error" ? "up-error" : "up-note";
  }

  /** The server's message under the row, without repeating the row's state. */
  function errorNote(state, message) {
    let text = String(message || "").trim();
    if (state && text.toLowerCase().startsWith(state.toLowerCase())) text = text.slice(state.length).replace(/^[\s:;,.-]+/, "");
    if (text && !/[.!?]$/.test(text)) text += ".";
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function addFiles(list) {
    const files = [...list];
    const images = files.filter(isImage);
    const skipped = files.length - images.length;
    if (skipped) toast(`Skipped ${skipped} file${skipped === 1 ? "" : "s"} that ${skipped === 1 ? "is not an image" : "are not images"}.`, "warn");
    if (images.length) queue.add(images);
  }

  fileInput.addEventListener("change", () => {
    addFiles(fileInput.files || []);
    fileInput.value = ""; // picking the same file again fires change again
  });
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fileInput.click();
    }
  });

  // dropping anywhere on the page works; the drop zone lights up
  let dragDepth = 0;
  const hasFiles = (e) => e.dataTransfer && [...(e.dataTransfer.types || [])].includes("Files");
  const onDragEnter = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth += 1;
    dropzone.classList.add("over");
  };
  const onDragOver = (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  };
  const onDragLeave = () => {
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) dropzone.classList.remove("over");
  };
  const onDrop = (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    dragDepth = 0;
    dropzone.classList.remove("over");
    addFiles(e.dataTransfer.files);
  };
  document.addEventListener("dragenter", onDragEnter);
  document.addEventListener("dragover", onDragOver);
  document.addEventListener("dragleave", onDragLeave);
  document.addEventListener("drop", onDrop);

  // --- grid ---------------------------------------------------------------------------

  function updateSub() {
    const excluded = photos.filter((p) => p.excluded).length;
    const n = photos.length;
    sub.textContent = n
      ? `${n} photo${n === 1 ? "" : "s"}${excluded ? ` · ${excluded} excluded from the rotation` : ""}`
      : "Upload photos and pick how each one is dithered";
  }

  function tile(photo, { added = false } = {}) {
    const method = photo.recipe_effective && photo.recipe_effective.method;
    const name = displayName(photo.original_name);
    const img = h("img", { src: photo.render_url, width: 600, height: 400, alt: "", loading: "lazy", decoding: "async" });
    // "rendering" only when the frame really has to dither it first; an
    // existing render that is merely loading gets a plain placeholder
    const media = h("a", { class: "tile-media", href: `#/photos/${photo.id}`, "aria-label": `Edit ${name}`, dataset: photo.render_ready ? null : { pending: "" } }, img);
    img.addEventListener("load", () => media.classList.add("loaded"));
    img.addEventListener("error", () => media.classList.add("broken"));
    const badges = h("div", { class: "tile-badges" },
      photo.portrait ? h("span", { class: "badge badge-warn", title: "Portrait photos are letterboxed with white bars; the frame is landscape." }, "Portrait") : null,
      photo.excluded ? h("span", { class: "badge badge-muted", title: "Not shown in the rotation" }, "Excluded") : null,
    );
    media.append(badges);

    // the actions say what they do: a labelled show (the label from 640 px),
    // an explicit Exclude / Include toggle, and a red delete at the far end
    // (labelled from 1024 px, where the tiles are wide enough)
    const showBtn = h("button", { class: "tile-btn", type: "button", title: "Show on the panel now", "aria-label": `Show ${name} on the panel now`, onclick: () => showNow(photo) }, icon("show"), h("span", { class: "tile-btn-wide" }, "Show"));
    const excludeBtn = h("button", {
      class: "tile-btn tile-btn-text", type: "button",
      title: photo.excluded ? "Put it back in the rotation" : "Keep it out of the rotation (it stays here, and show-now still works)",
      "aria-label": photo.excluded ? `Include ${name} in the rotation` : `Exclude ${name} from the rotation`,
      onclick: () => toggleExcluded(photo),
    }, photo.excluded ? "Include" : "Exclude");
    const deleteBtn = h("button", { class: "tile-btn tile-btn-danger", type: "button", title: "Delete", "aria-label": `Delete ${name}`, onclick: () => remove(photo) }, icon("trash"), h("span", { class: "tile-btn-xl" }, "Delete"));

    return h("article", { class: `tile${photo.excluded ? " excluded" : ""}${added ? " added" : ""}`, role: "listitem", dataset: { id: photo.id } },
      media,
      h("div", { class: "tile-meta" },
        h("a", { class: "tile-name", href: `#/photos/${photo.id}`, title: photo.original_name }, name),
        h("span", { class: "tile-method mono" }, (methodNames[method] || method || "") + (photo.recipe ? "" : " · default"))),
      h("div", { class: "tile-actions" }, showBtn, excludeBtn, deleteBtn),
    );
  }

  function renderGrid({ added = null } = {}) {
    updateSub();
    dropzone.classList.toggle("dropzone-hero", photos.length === 0);
    if (!photos.length) {
      body.replaceChildren(emptyState("No photos yet", "Add a few landscape photos. Each one is dithered to the panel's six colours as it arrives, and you can pick a different method for it later."));
      return;
    }
    grid.replaceChildren(...photos.map((p) => tile(p, { added: p.id === added })));
    body.replaceChildren(grid);
  }

  function replacePhoto(photo) {
    const i = photos.findIndex((p) => p.id === photo.id);
    if (i < 0) return;
    photos[i] = photo;
    const old = grid.querySelector(`[data-id="${photo.id}"]`);
    if (old) old.replaceWith(tile(photo));
    updateSub();
  }

  async function showNow(photo) {
    try {
      const result = await api("/api/display/show", { method: "POST", body: { photo_id: photo.id } });
      toast(queuedMessage(result, displayName(photo.original_name)), "ok");
    } catch {
      // toasted
    }
  }

  async function toggleExcluded(photo) {
    try {
      const updated = await api(`/api/photos/${photo.id}/flags`, { method: "PUT", body: { excluded: !photo.excluded } });
      replacePhoto(updated);
      const name = displayName(updated.original_name);
      toast(updated.excluded ? `${name} is out of the rotation.` : `${name} is back in the rotation.`, "ok");
    } catch {
      // toasted
    }
  }

  async function remove(photo) {
    const ok = await confirmDialog({
      title: `Delete “${displayName(photo.original_name)}”?`,
      message: "The photo and all of its renders are removed from the frame. This cannot be undone.",
      confirm: "Delete photo",
      danger: true,
      image: photo.render_url,
    });
    if (!ok) return;
    try {
      await api(`/api/photos/${photo.id}`, { method: "DELETE" });
      photos = photos.filter((p) => p.id !== photo.id);
      const el = grid.querySelector(`[data-id="${photo.id}"]`);
      if (el && photos.length) {
        el.remove();
        updateSub();
      } else {
        renderGrid();
      }
      toast(`Deleted ${displayName(photo.original_name)}.`, "ok");
    } catch {
      // toasted
    }
  }

  // --- load ---------------------------------------------------------------------------

  Promise.all([api("/api/photos"), getRecipeMeta().catch(() => null)])
    .then(([list, meta]) => {
      if (destroyed) return;
      photos = list.photos;
      if (meta) methodNames = Object.fromEntries(meta.methods.map((m) => [m.id, m.name]));
      renderGrid();
    })
    .catch(() => {
      if (destroyed) return;
      body.replaceChildren(emptyState("Could not load the photos", "The frame did not answer. Check that it is on the network, then reload."));
    });

  return {
    destroy() {
      destroyed = true;
      document.removeEventListener("dragenter", onDragEnter);
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
      // uploads already running finish in the background; the grid shows
      // them the next time the page opens
    },
  };
}

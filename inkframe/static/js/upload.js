// The upload pipeline (plan section 5): downscale in the browser, then send
// one file per request with real progress, two at a time.  Decoding runs
// one file at a time: a full-size bitmap of a 24 MP phone photo is about
// 96 MB, and two at once is more than an iPhone tab may hold.
//
//   file --decode--> bitmap --canvas, long edge <= 1800 px--> JPEG q0.92 --XHR--> 201 photo
//
// Decoding applies the EXIF orientation first (createImageBitmap with
// imageOrientation "from-image"; an <img> does the same by default), so a
// phone photo taken sideways arrives upright.  When the browser cannot decode
// the file at all (HEIC in Chrome or Firefox) the original bytes are sent
// unchanged and the server converts them with pillow-heif.

export const LONG_EDGE = 1800;
export const JPEG_QUALITY = 0.92;
export const CONCURRENCY = 2;

/** Decode with createImageBitmap, else an <img>; null when neither can. */
async function decode(file) {
  if (typeof createImageBitmap === "function") {
    try {
      return await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      // some engines reject the options bag or the format: try the <img> path
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    if (!img.naturalWidth || !img.naturalHeight) return null;
    return img;
  } catch {
    return null;
  } finally {
    // the decoded <img> keeps its pixels after the URL is revoked
    URL.revokeObjectURL(url);
  }
}

function sizeOf(source) {
  return {
    width: source.naturalWidth || source.width,
    height: source.naturalHeight || source.height,
  };
}

function toBlob(canvas, type, quality) {
  return new Promise((resolve) => canvas.toBlob(resolve, type, quality));
}

function jpegName(name) {
  const base = String(name || "photo").replace(/\.[^./\\]+$/, "");
  return `${base || "photo"}.jpg`;
}

/**
 * Prepare a picked file for upload.  Resolves with
 * {blob, filename, width, height, converted}: `converted` is false when the
 * original is sent unchanged (it could not be decoded here).
 */
export async function prepareImage(file) {
  const source = await decode(file);
  if (!source) {
    return { blob: file, filename: file.name || "photo", width: null, height: null, converted: false };
  }
  try {
    const { width, height } = sizeOf(source);
    // never upscale: a small image is re-encoded at its own size
    const scale = Math.min(1, LONG_EDGE / Math.max(width, height));
    const w = Math.max(1, Math.round(width * scale));
    const hgt = Math.max(1, Math.round(height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = hgt;
    const ctx = canvas.getContext("2d");
    // transparent PNGs land on the panel's white, like the letterbox bars
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, w, hgt);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(source, 0, 0, w, hgt);
    const blob = await toBlob(canvas, "image/jpeg", JPEG_QUALITY);
    // free the canvas backing store now: iOS caps the total canvas memory
    canvas.width = 0;
    canvas.height = 0;
    if (!blob) {
      return { blob: file, filename: file.name || "photo", width, height, converted: false };
    }
    return { blob, filename: jpegName(file.name), width: w, height: hgt, converted: true };
  } finally {
    if (typeof source.close === "function") source.close();
  }
}

/**
 * POST one prepared file as multipart field "file" (with the display name in
 * "name").  XHR rather than fetch because only XHR reports upload progress.
 * Returns {promise, abort}; the promise resolves with the photo object or
 * rejects with an Error carrying the server's message and `status`.
 *
 * onProgress(fraction) runs while the body is sent; onSent() once the body
 * has gone and the server is decoding and dithering ("rendering").
 */
export function sendPhoto({ blob, filename, name }, { onProgress, onSent } = {}) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", blob, filename);
    if (name) form.append("name", name);
    xhr.open("POST", "/api/photos");
    xhr.responseType = "text";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total);
    };
    xhr.upload.onload = () => {
      if (onProgress) onProgress(1);
      if (onSent) onSent();
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        data = null;
      }
      if (xhr.status === 201 && data) {
        resolve(data);
        return;
      }
      const err = new Error((data && data.error) || `Upload failed (${xhr.status} ${xhr.statusText})`.trim());
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => {
      const err = new Error("The upload was interrupted. Is the frame on the network?");
      err.status = 0;
      reject(err);
    };
    xhr.onabort = () => {
      const err = new Error("Cancelled");
      err.status = -1;
      reject(err);
    };
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}

/**
 * A queue that runs at most CONCURRENCY uploads at once.  Each job moves
 * through states: queued -> preparing -> uploading (progress 0..1) ->
 * rendering -> done | error.  onChange(job) runs on every change; the job
 * object is the single source of truth for its row in the UI.
 */
export class UploadQueue {
  constructor({ onChange, onDone, concurrency = CONCURRENCY } = {}) {
    this.onChange = onChange || (() => {});
    this.onDone = onDone || (() => {});
    this.concurrency = concurrency;
    this.jobs = [];
    this.active = 0;
    this.nextId = 1;
    this.decoding = Promise.resolve(); // the tail of the one-at-a-time decode chain
  }

  add(files) {
    for (const file of files) {
      const job = {
        id: this.nextId++,
        file,
        name: file.name || "photo",
        size: file.size,
        state: "queued",
        progress: 0,
        error: null,
        photo: null,
        abort: null,
      };
      this.jobs.push(job);
      this.onChange(job);
    }
    this.pump();
  }

  cancel(job) {
    if (job.abort) job.abort();
    else if (job.state === "queued" || job.state === "preparing") {
      this.update(job, { state: "error", error: "Cancelled", status: -1 });
    }
  }

  update(job, patch) {
    Object.assign(job, patch);
    this.onChange(job);
  }

  pump() {
    while (this.active < this.concurrency) {
      const job = this.jobs.find((j) => j.state === "queued" && !j.started);
      if (!job) return;
      job.started = true;
      this.active += 1;
      this.run(job).finally(() => {
        this.active -= 1;
        this.pump();
      });
    }
  }

  /**
   * Decode and downscale `job`'s file after every earlier decode has
   * finished.  The job stays "queued" (the row says Waiting) until its turn.
   * Resolves null when it was cancelled before or during its turn.
   */
  prepare(job) {
    const turn = this.decoding.then(async () => {
      if (job.state !== "queued") return null;
      this.update(job, { state: "preparing" });
      const prepared = await prepareImage(job.file);
      return job.state === "preparing" ? prepared : null;
    });
    // the next decode waits for this one, whether it worked or not
    this.decoding = turn.catch(() => null);
    return turn;
  }

  async run(job) {
    try {
      const prepared = await this.prepare(job);
      if (!prepared) return; // cancelled meanwhile
      this.update(job, { state: "uploading", progress: 0, converted: prepared.converted, bytes: prepared.blob.size });
      const { promise, abort } = sendPhoto(
        { ...prepared, name: job.name },
        {
          onProgress: (p) => this.update(job, { progress: p }),
          onSent: () => this.update(job, { state: "rendering", progress: 1 }),
        },
      );
      job.abort = abort;
      const photo = await promise;
      job.abort = null;
      this.update(job, { state: "done", photo });
      this.onDone(job);
    } catch (e) {
      job.abort = null;
      this.update(job, { state: "error", error: e.message || String(e), status: e.status });
    }
  }
}

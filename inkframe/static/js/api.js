// The JSON API client: one fetch helper that turns {"error": "..."} answers
// into toasts (or hands them to the caller with {quiet: true}), a connection
// state the navigation shows, and a server-clock offset for countdowns.

import { toast } from "./ui.js";

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

// --- connection state ----------------------------------------------------------

let online = null;
const listeners = new Set();

export function onConnection(fn) {
  listeners.add(fn);
  if (online !== null) fn(online);
  return () => listeners.delete(fn);
}

function setOnline(value) {
  if (online === value) return;
  online = value;
  for (const fn of listeners) fn(value);
}

// --- server clock ----------------------------------------------------------------

// The countdowns compare server timestamps with the phone's clock.  The Date
// header (1 s resolution) tells us how far apart the two are; small
// differences are ignored, large ones (a Pi without network time) corrected.
let clockOffset = 0;

function noteServerDate(res) {
  const header = res.headers.get("date");
  if (!header) return;
  const server = Date.parse(header);
  if (Number.isNaN(server)) return;
  const offset = server - Date.now();
  clockOffset = Math.abs(offset) > 3000 ? offset : 0;
}

/** Now, on the server's clock (ms since the epoch). */
export function serverNow() {
  return Date.now() + clockOffset;
}

// --- requests ----------------------------------------------------------------------

/**
 * api("/api/photos")                          GET, parsed JSON
 * api(url, {method: "PUT", body: {...}})      JSON body
 * api(url, {quiet: true})                     errors are thrown, not toasted
 *
 * Resolves with the parsed body (null for 204).  Rejects with an ApiError
 * whose message is the server's "error" text, or a network message.
 */
export async function api(path, { method = "GET", body, quiet = false, signal } = {}) {
  const init = { method, cache: "no-store", signal };
  if (body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    setOnline(false);
    const err = new ApiError("Cannot reach the frame. Is it on the network?", 0, null);
    if (!quiet) toast(err.message, "error");
    throw err;
  }
  setOnline(true);
  noteServerDate(res);
  if (res.status === 204) return null;
  const text = await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!res.ok) {
    const message = (data && typeof data.error === "string" && data.error) || `${res.status} ${res.statusText}`.trim();
    const err = new ApiError(message, res.status, data);
    if (!quiet) toast(message, "error");
    throw err;
  }
  return data;
}

/** GET an image as an object URL so a failure can show the server's message. */
export async function fetchImage(path, { signal } = {}) {
  let res;
  try {
    res = await fetch(path, { cache: "no-store", signal });
  } catch (e) {
    if (e && e.name === "AbortError") throw e;
    setOnline(false);
    throw new ApiError("Cannot reach the frame. Is it on the network?", 0, null);
  }
  setOnline(true);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`.trim();
    try {
      const data = await res.json();
      if (data && data.error) message = data.error;
    } catch {
      // not JSON: keep the status line
    }
    throw new ApiError(message, res.status, null);
  }
  return URL.createObjectURL(await res.blob());
}

// --- shared, rarely-changing data ----------------------------------------------------

let recipeMeta = null;

/** The default recipe, the method registry and the knob limits (cached). */
export function getRecipeMeta({ fresh = false } = {}) {
  if (!recipeMeta || fresh) {
    recipeMeta = api("/api/recipe/default").catch((e) => {
      recipeMeta = null;
      throw e;
    });
  }
  return recipeMeta;
}

export function setRecipeMeta(data) {
  recipeMeta = Promise.resolve(data);
}

/** Toast-friendly sentence for a show-now answer ({rotation: {pending}}). */
export function queuedMessage(result, what) {
  const pending = result && result.rotation && result.rotation.pending;
  if (!pending || !pending.fires_at) return `${what} is queued for the panel.`;
  const secs = Math.max(0, Math.round((Date.parse(pending.fires_at) - serverNow()) / 1000));
  if (secs <= 2) return `${what} goes to the panel now; a refresh takes about 20 s.`;
  if (secs < 90) return `${what} goes to the panel in ${secs} s.`;
  return `${what} goes to the panel in ${Math.round(secs / 60)} min (the panel needs a rest between refreshes).`;
}
